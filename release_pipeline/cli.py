"""命令行入口：version / build / release / rollback / env，本地与 CI 通用。

同一条命令在本地与 CI 中行为一致；profile 只影响输出元数据，不影响判定。
所有路径默认相对当前目录，可用 --repo-root / --work-root 覆盖。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .builder import load_specs
from .errors import EnvironmentRejected, PipelineError
from .pipeline import (
    EXIT_ENV_REJECTED,
    EXIT_OK,
    EXIT_UNEXPECTED,
    PipelineConfig,
    run,
)
from .rollout import STAGE_ORDER
from .runtime import detect_profile, enforce_capabilities
from .versioning import derive_version

DEFAULT_TARGETS = ("canary", "partial-a", "partial-b", "full")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-pipeline",
        description="本地可复现的发布自动化流水线（版本/构建/缓存/分阶段上线）",
    )
    parser.add_argument("--repo-root", default=".", help="仓库根目录（默认当前目录）")
    parser.add_argument("--work-root", default=".release-work", help="工作目录（缓存/产物/发布状态）")
    parser.add_argument("--config", default=None,
                        help="构建矩阵配置（相对路径基于 --repo-root）；缺省用内置四目标矩阵")
    parser.add_argument("--verbose", action="store_true", help="打印关键判定日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="推导并打印版本与变更记录")
    p_version.add_argument("--set-version", dest="set_version", default=None,
                           help="显式版本 X.Y.Z（优先级最高，冲突会在输出中记录）")
    p_version.add_argument("--json", action="store_true", help="以 JSON 输出")

    p_build = sub.add_parser("build", help="执行多目标构建矩阵")
    p_build.add_argument("--no-cache", action="store_true")
    p_build.add_argument("--max-workers", type=int, default=4)
    p_build.add_argument("--json", action="store_true")

    p_release = sub.add_parser("release", help="构建并分阶段上线（失败自动回滚）")
    p_release.add_argument("--no-cache", action="store_true")
    p_release.add_argument("--no-rollout", action="store_true", help="只构建不上线")
    p_release.add_argument("--stage", action="append", dest="stages",
                           choices=[s.value for s in STAGE_ORDER],
                           help="指定按顺序推进的阶段，可重复给出")
    p_release.add_argument("--set-version", dest="set_version", default=None)
    p_release.add_argument("--max-workers", type=int, default=4)
    p_release.add_argument("--json", action="store_true")

    p_env = sub.add_parser("env", help="打印执行环境画像与能力门禁结论")
    p_env.add_argument("--json", action="store_true")

    return parser


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _resolve_config_path(repo_root: Path, config: str, *, explicit: bool) -> Path:
    p = Path(config)
    if not p.is_absolute():
        p = repo_root / p
    if not p.is_file():
        if explicit:
            raise PipelineError(
                f"显式指定的构建配置不存在: {p}（拒绝静默回退到默认目标）",
                details={"config": str(p)},
            )
        return p  # 默认路径，调用方按“不存在则用内置默认矩阵”处理
    return p


def _load_specs(config_path: Path, *, explicit: bool) -> list:
    from .models import BuildSpec

    if config_path.is_file():
        return load_specs(config_path)
    if explicit:
        raise PipelineError(f"构建配置不存在: {config_path}")
    return [BuildSpec(target=t, command="sim:ok") for t in DEFAULT_TARGETS]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    repo_root = Path(args.repo_root)
    work_root = Path(args.work_root)

    try:
        if args.command == "env":
            profile = detect_profile()
            rejected = enforce_capabilities(
                profile, require_cache=True, require_rollback=True
            )
            payload = {
                "profile": profile.profile,
                "ci_provider": profile.ci_provider,
                "python_version": profile.python_version,
                "git_available": profile.git_available,
                "capabilities": [
                    {"capability": c.capability, "supported": c.supported, "reason": c.reason}
                    for c in profile.capabilities
                ],
                "rejected": [
                    {"capability": c.capability, "reason": c.reason} for c in rejected
                ],
                "normalized_env": profile.normalized,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return EXIT_OK if not rejected else EXIT_ENV_REJECTED

        if args.command == "version":
            info = derive_version(repo_root, explicit_version=args.set_version)
            if args.json:
                print(json.dumps({
                    "version": info.version,
                    "commit": info.commit,
                    "source": info.source,
                    "resolution": info.resolution,
                    "sources": info.sources,
                    "dirty": info.dirty,
                    "changelog": list(info.changelog),
                }, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(info.version)
                for line in info.changelog:
                    print(f"  {line}", file=sys.stderr)
            return EXIT_OK

        config_path = _resolve_config_path(
            repo_root, args.config or "release.toml", explicit=args.config is not None
        )
        specs = _load_specs(config_path, explicit=args.config is not None)

        if args.command == "build":
            outcome = run(PipelineConfig(
                repo_root=repo_root,
                work_root=work_root,
                specs=specs,
                cache_enabled=not args.no_cache,
                rollout_enabled=False,
                max_workers=args.max_workers,
            ))
            _print_outcome(outcome, as_json=args.json)
            return outcome.exit_code

        if args.command == "release":
            outcome = run(PipelineConfig(
                repo_root=repo_root,
                work_root=work_root,
                specs=specs,
                cache_enabled=not args.no_cache,
                rollout_enabled=not args.no_rollout,
                stages=args.stages,
                explicit_version=args.set_version,
                max_workers=args.max_workers,
            ))
            _print_outcome(outcome, as_json=args.json)
            return outcome.exit_code

    except EnvironmentRejected as exc:
        print(f"环境拒绝: {exc.reason}", file=sys.stderr)
        return EXIT_ENV_REJECTED
    except PipelineError as exc:
        print(f"流水线错误: {exc.reason} 细节={exc.details}", file=sys.stderr)
        return EXIT_UNEXPECTED
    return EXIT_UNEXPECTED


def _print_outcome(outcome, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "exit_code": outcome.exit_code,
            "summary": outcome.summary,
            "error": outcome.error,
            "version": outcome.version.version if outcome.version else None,
            "build": None
            if outcome.build is None
            else {
                "conclusion": outcome.build.conclusion.value,
                "successful": list(outcome.build.successful),
                "failed": list(outcome.build.failed),
                "skipped": list(outcome.build.skipped),
                "results": [
                    {
                        "target": r.target,
                        "status": r.status.value,
                        "cache": r.cache_status.value if r.cache_status else None,
                        "artifact": r.artifact_path,
                        "digest": r.artifact_digest,
                    }
                    for r in outcome.build.results
                ],
            },
            "rollout": None
            if outcome.rollout is None
            else {
                "final_state": outcome.rollout.final_state.value,
                "rolled_back": outcome.rollout.rolled_back,
                "reached_stage": outcome.rollout.reached_stage.value
                if outcome.rollout.reached_stage else None,
                "reason": outcome.rollout.reason,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"[exit={outcome.exit_code}] {outcome.summary}")
        if outcome.build is not None:
            for r in outcome.build.results:
                cache = r.cache_status.value if r.cache_status else "-"
                print(f"  - {r.target}: {r.status.value} (cache={cache}) {r.log}")
        if outcome.rollout is not None and outcome.rollout.stages:
            for s in outcome.rollout.stages:
                print(f"  stage {s.stage.value}: {s.state.value} rolled_back={s.rolled_back}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
