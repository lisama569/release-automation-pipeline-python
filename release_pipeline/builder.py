"""多目标构建矩阵：本地可复现的模拟构建 + 真实命令构建。

模拟命令（无需外部服务）：
- ``sim:ok``          成功；选项 ``health=ok|fail|ambiguous|inconsistent``
  控制随产物生成的 health.json，供上线阶段健康门使用。
- ``sim:fail:<阶段>`` 在 spec / build / package 阶段确定性失败，用于异常链路测试。

真实命令：``cmd:-- <argv...>``，在临时工作区执行；非零退出 => 目标 FAILED。

单个目标的成败完全隔离：任何异常都收敛为该目标的 BuildResult(FAILED)，
不会波及其他目标，也不会被记成其他目标的结果。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .cache import BuildCache, CacheEntry, compute_cache_key
from .errors import BuildFailure, PipelineError
from .locking import temp_workdir
from .models import (
    BuildResult,
    BuildSpec,
    CacheStatus,
    MatrixConclusion,
    MatrixReport,
    TargetStatus,
)

logger = logging.getLogger(__name__)

_VALID_HEALTH = {"ok", "fail", "ambiguous", "inconsistent"}


def digest_spec(spec: BuildSpec) -> str:
    """构建规格摘要（命令、选项均纳入，命令变化必然失效缓存）。"""
    h = hashlib.sha256()
    h.update(spec.target.encode("utf-8"))
    h.update(b"\x00")
    h.update(spec.command.encode("utf-8"))
    h.update(b"\x00")
    for rel in sorted(spec.inputs):
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
    for k in sorted(spec.options):
        h.update(f"{k[0]}={k[1]}".encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _options_map(spec: BuildSpec) -> dict[str, str]:
    return dict(spec.options)


def build_target(
    spec: BuildSpec,
    *,
    version: str,
    repo_root: Path,
    work_root: Path,
    cache: BuildCache | None,
    env_digest: str,
    sources_digest: str,
) -> BuildResult:
    """构建单个目标：优先复用缓存，否则执行构建并写入缓存。"""
    started = time.monotonic()
    repo_root = Path(repo_root)
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    spec_digest = digest_spec(spec)
    cache_key = compute_cache_key(
        target=spec.target,
        version=version,
        spec_digest=spec_digest,
        sources_digest=sources_digest,
        env_digest=env_digest,
    )
    logger.info(
        "build target=%s version=%s cache_key=%s 输入: 命令=%r 选项=%s",
        spec.target,
        version,
        cache_key[:12],
        spec.command,
        dict(spec.options),
    )

    # 本线程本次构建的缓存判定；不使用 cache 对象上的共享状态
    lookup_status: CacheStatus | None = None
    try:
        if cache is not None:
            entry, lookup_status = cache.lookup(cache_key)
            if entry is not None:
                return BuildResult(
                    target=spec.target,
                    status=TargetStatus.SUCCESS,
                    cache_status=lookup_status,
                    version=version,
                    cache_key=cache_key,
                    artifact_path=entry.artifact_path,
                    artifact_digest=entry.artifact_digest,
                    log="缓存命中：元数据与产物摘要校验通过",
                    duration_ms=_elapsed_ms(started),
                )

        artifact_path, artifact_digest, log = _execute(
            spec, version=version, repo_root=repo_root, work_root=work_root
        )

        if cache is not None:
            cache.store(
                CacheEntry(
                    cache_key=cache_key,
                    target=spec.target,
                    version=version,
                    artifact_path=artifact_path,
                    artifact_digest=artifact_digest,
                    env_digest=env_digest,
                    spec_digest=spec_digest,
                    sources_digest=sources_digest,
                )
            )
        return BuildResult(
            target=spec.target,
            status=TargetStatus.SUCCESS,
            cache_status=lookup_status,
            version=version,
            cache_key=cache_key,
            artifact_path=artifact_path,
            artifact_digest=artifact_digest,
            log=log,
            duration_ms=_elapsed_ms(started),
        )
    except BuildFailure as exc:
        logger.warning("build target=%s 判定=FAILED 依据=%s", spec.target, exc.reason)
        return BuildResult(
            target=spec.target,
            status=TargetStatus.FAILED,
            cache_status=lookup_status,
            version=version,
            cache_key=cache_key,
            artifact_path=None,
            artifact_digest=None,
            log=f"构建失败: {exc.reason} | 细节: {exc.details}",
            duration_ms=_elapsed_ms(started),
        )
    except Exception as exc:  # noqa: BLE001 - 单目标异常隔离，绝不让矩阵崩溃
        logger.exception("build target=%s 判定=FAILED 依据=未预期异常", spec.target)
        return BuildResult(
            target=spec.target,
            status=TargetStatus.FAILED,
            cache_status=lookup_status,
            version=version,
            cache_key=cache_key,
            artifact_path=None,
            artifact_digest=None,
            log=f"构建异常: {type(exc).__name__}: {exc}",
            duration_ms=_elapsed_ms(started),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _execute(
    spec: BuildSpec, *, version: str, repo_root: Path, work_root: Path
) -> tuple[str, str, str]:
    """实际执行构建，返回 (产物绝对路径, 产物摘要, 日志)。失败统一抛 BuildFailure。"""
    command = spec.command.strip()

    if command == "sim:fail:spec":
        raise BuildFailure(
            "规格阶段确定性失败（sim:fail:spec）",
            details={"target": spec.target, "stage": "spec"},
        )

    if command.startswith("sim:fail:"):
        stage = command.split(":", 2)[2] or "build"

    with temp_workdir(work_root, prefix=f"build-{spec.target}-") as workdir:
        if command == "sim:ok":
            health = _options_map(spec).get("health", "ok")
            if health not in _VALID_HEALTH:
                raise BuildFailure(
                    f"未知 health 选项 {health!r}，合法值 {sorted(_VALID_HEALTH)}",
                    details={"target": spec.target},
                )
            return _sim_build(spec, version, workdir, health=health)

        if command.startswith("sim:fail:"):
            return _sim_fail(spec, version, workdir, stage=stage)

        if command.startswith("cmd:"):
            return _cmd_build(spec, version, repo_root, workdir, command)

        raise BuildFailure(
            f"未知构建命令 {command!r}（支持 sim:ok / sim:fail:<stage> / cmd:-- ...）",
            details={"target": spec.target},
        )


def _sim_build(
    spec: BuildSpec, version: str, workdir: Path, *, health: str
) -> tuple[str, str, str]:
    manifest = {
        "target": spec.target,
        "version": version,
        "built_by": "release-pipeline/sim",
        "options": dict(spec.options),
    }
    (workdir / "payload").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    if health != "ambiguous":
        # 无 health.json => 探针读取不到清单，结论不明确（ambiguous 由缺文件模拟）
        health_manifest = {
            "status": "fail" if health == "fail" else "ok",
            "attempts_required": 3,
            "inconsistent": health == "inconsistent",
        }
        (workdir / "health.json").write_text(
            json.dumps(health_manifest, sort_keys=True), encoding="utf-8"
        )

    artifact = _make_bundle(spec, version, workdir)
    digest = _sha256_file(artifact)
    log = f"模拟构建成功 health={health}，产物 sha256={digest[:12]}"
    logger.info("sim build target=%s health=%s digest=%s", spec.target, health, digest[:12])
    return str(artifact), digest, log


def _make_bundle(spec: BuildSpec, version: str, workdir: Path) -> Path:
    """把工作区中的 payload/health.json 打成确定性 tar bundle，移到持久产物区。

    成员固定、mtime 固定 => 同内容字节级可复现，缓存摘要稳定。
    """
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        for name in ("health.json", "payload"):
            q = workdir / name
            if not q.exists():
                continue
            data = q.read_bytes()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    persistent = workdir.parent / f"artifact-{spec.target}-{version}-{digest[:12]}.tar"
    persistent.write_bytes(raw)
    return persistent


def _sim_fail(
    spec: BuildSpec, version: str, workdir: Path, *, stage: str
) -> tuple[str, str, str]:
    if stage == "build":
        raise BuildFailure(
            "构建阶段确定性失败（sim:fail:build）",
            details={"target": spec.target, "stage": stage, "version": version},
        )
    # package 阶段：先产出半成品再失败；临时目录随上下文管理器清理，不留可用产物
    (workdir / "half-product").write_text("incomplete", encoding="utf-8")
    raise BuildFailure(
        f"打包阶段确定性失败（sim:fail:{stage}），已丢弃半成品",
        details={"target": spec.target, "stage": stage},
    )


def _cmd_build(
    spec: BuildSpec, version: str, repo_root: Path, workdir: Path, command: str
) -> tuple[str, str, str]:
    body = command[len("cmd:"):]
    if body.startswith("--"):
        body = body[2:].strip()
    argv = body.split()
    if not argv:
        raise BuildFailure("cmd: 构建命令为空", details={"target": spec.target})
    env = os.environ.copy()
    env["RELEASE_VERSION"] = version
    env["RELEASE_TARGET"] = spec.target
    proc = subprocess.run(
        argv,
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise BuildFailure(
            f"构建命令退出码 {proc.returncode}: {proc.stderr.strip()[:500]}",
            details={"target": spec.target, "argv": argv},
        )
    if not (workdir / "payload").is_file():
        raise BuildFailure(
            "构建命令成功但未产出 payload 文件（真实命令须写出 payload 与可选 health.json）",
            details={"target": spec.target, "argv": argv},
        )
    if not (workdir / "health.json").is_file():
        # 命令未显式给清单时不替用户猜测失败，默认健康；要模拟不明确就不写该文件
        (workdir / "health.json").write_text(
            json.dumps({"status": "ok", "attempts_required": 1, "inconsistent": False}),
            encoding="utf-8",
        )
    artifact = _make_bundle(spec, version, workdir)
    digest = _sha256_file(artifact)
    return str(artifact), digest, f"命令构建成功 argv={argv}"




def run_matrix(
    specs: list[BuildSpec],
    *,
    version: str,
    repo_root: Path,
    work_root: Path,
    cache: BuildCache | None,
    env_digest: str,
    sources_digest: str,
    max_workers: int = 4,
) -> MatrixReport:
    """并发执行矩阵；单目标失败不影响其他目标，汇总结论可区分。"""
    names = [s.target for s in specs]
    if len(names) != len(set(names)):
        raise PipelineError("构建矩阵存在重复目标名，拒绝执行（结果无法归属）")

    results_by_target: dict[str, BuildResult] = {}
    if not specs:
        return MatrixReport(
            results=(),
            conclusion=MatrixConclusion.EMPTY,
            successful=(),
            failed=(),
            skipped=(),
        )

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(
                build_target,
                spec,
                version=version,
                repo_root=repo_root,
                work_root=work_root / spec.target,
                cache=cache,
                env_digest=env_digest,
                sources_digest=sources_digest,
            ): spec.target
            for spec in specs
        }
        for fut in as_completed(futures):
            target = futures[fut]
            results_by_target[target] = fut.result()

    # 按输入规格顺序输出，保证报告确定性
    results = tuple(results_by_target[s.target] for s in specs)
    successful = tuple(r.target for r in results if r.status is TargetStatus.SUCCESS)
    failed = tuple(r.target for r in results if r.status is TargetStatus.FAILED)
    skipped = tuple(r.target for r in results if r.status is TargetStatus.SKIPPED)

    if failed and successful:
        conclusion = MatrixConclusion.PARTIAL_FAILURE
    elif failed:
        conclusion = MatrixConclusion.FAILURE
    else:
        conclusion = MatrixConclusion.SUCCESS

    report = MatrixReport(
        results=results,
        conclusion=conclusion,
        successful=successful,
        failed=failed,
        skipped=skipped,
    )
    logger.info(
        "matrix 判定=%s 成功=%s 失败=%s 跳过=%s",
        conclusion.value,
        successful,
        failed,
        skipped,
    )
    return report


def load_specs(path: Path) -> list[BuildSpec]:
    """从 release.toml 加载构建目标；非法配置直接报错，绝不静默忽略目标。"""
    import tomllib

    path = Path(path)
    if not path.is_file():
        raise PipelineError(f"构建配置不存在: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise PipelineError("release.toml 中 targets 必须是非空数组")
    specs: list[BuildSpec] = []
    seen: set[str] = set()
    for item in raw_targets:
        if not isinstance(item, dict):
            raise PipelineError(f"非法目标条目（非表）: {item!r}")
        target = item.get("name")
        command = item.get("command")
        if not isinstance(target, str) or not target.strip():
            raise PipelineError(f"目标缺少合法 name: {item!r}")
        if not isinstance(command, str) or not command.strip():
            raise PipelineError(f"目标 {target} 缺少合法 command")
        if target in seen:
            raise PipelineError(f"重复目标名: {target}")
        seen.add(target)
        inputs = item.get("inputs", [])
        if not isinstance(inputs, list) or not all(isinstance(x, str) for x in inputs):
            raise PipelineError(f"目标 {target} 的 inputs 必须是字符串数组")
        options_raw = item.get("options", {})
        if not isinstance(options_raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str | int | bool)
            for k, v in options_raw.items()
        ):
            raise PipelineError(f"目标 {target} 的 options 必须是字符串键值表")
        options = tuple(sorted((k, str(v)) for k, v in options_raw.items()))
        specs.append(
            BuildSpec(
                target=target,
                command=command,
                inputs=tuple(inputs),
                options=options,
            )
        )
    return specs
