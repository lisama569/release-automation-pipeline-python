"""流水线编排：版本 -> 矩阵构建 -> 分阶段上线，对外唯一入口。

退出码约定（本地/CI 一致）：
- 0 全部成功（或全部构建成功且未启用上线）
- 10 构建矩阵部分失败：成功产物可用，失败目标明确列出
- 11 构建矩阵全部失败：无可用产物
- 12 构建矩阵为空：配置问题，明确拒绝
- 20 上线被健康门拦截并已自动回滚，对外状态恢复上线前
- 21 上线/回滚本身失败（状态不一致等），需人工介入
- 30 环境能力被拒绝（如不支持的参数/能力）
- 31 版本推导失败
- 1  其他未预期错误
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .builder import run_matrix
from .cache import BuildCache, digest_environment, digest_paths
from .errors import EnvironmentRejected, PipelineError, VersionError
from .locking import sweep_stale
from .models import MatrixConclusion, MatrixReport, ReleaseStage, VersionInfo
from .rollout import STAGE_ORDER, execute_rollout
from .models import RolloutReport
from .runtime import CACHE_ENV_WHITELIST, detect_profile, enforce_capabilities, normalize_environment
from .versioning import derive_version

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 10
EXIT_ALL_FAILED = 11
EXIT_EMPTY_MATRIX = 12
EXIT_ROLLED_BACK = 20
EXIT_ROLLOUT_ERROR = 21
EXIT_ENV_REJECTED = 30
EXIT_VERSION_ERROR = 31
EXIT_UNEXPECTED = 1


@dataclass
class PipelineConfig:
    repo_root: Path
    work_root: Path
    specs: list = field(default_factory=list)
    cache_enabled: bool = True
    rollout_enabled: bool = True
    stages: list[str] | None = None
    explicit_version: str | None = None
    max_workers: int = 4
    env: dict | None = None  # 可注入环境变量，便于本地/CI 一致性测试


@dataclass
class PipelineOutcome:
    version: VersionInfo | None
    build: MatrixReport | None
    rollout: RolloutReport | None
    exit_code: int
    summary: str
    error: str | None = None


def _resolve_stages(raw: list[str] | None) -> list[ReleaseStage]:
    if raw is None:
        return list(STAGE_ORDER)
    chosen: list[ReleaseStage] = []
    for name in raw:
        try:
            stage = ReleaseStage(name)
        except ValueError:
            raise EnvironmentRejected(
                f"不支持的上线阶段参数 {name!r}，合法值：{[s.value for s in STAGE_ORDER]}",
                details={"given": name},
            )
        if stage not in STAGE_ORDER:
            raise EnvironmentRejected(f"上线阶段 {name} 不在阶段计划中")
        chosen.append(stage)
    # 必须按计划顺序且构成前缀：不得跳过 canary 直接推进后续阶段
    prefix = list(STAGE_ORDER[: len(chosen)])
    if chosen != prefix:
        raise EnvironmentRejected(
            f"阶段必须构成 {[s.value for s in STAGE_ORDER]} 的有序前缀"
            f"（不得跳过 canary），收到 {raw}",
            details={"given": raw},
        )
    return chosen


def run(config: PipelineConfig) -> PipelineOutcome:
    """执行完整流水线；任何阶段都返回可解释的结论与明确退出码。"""
    repo_root = Path(config.repo_root)
    work_root = Path(config.work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    profile = detect_profile(config.env)
    logger.info(
        "环境画像 profile=%s ci_provider=%s python=%s git=%s",
        profile.profile, profile.ci_provider, profile.python_version, profile.git_available,
    )
    rejected = enforce_capabilities(
        profile,
        require_cache=config.cache_enabled,
        require_rollback=config.rollout_enabled,
    )
    if rejected:
        reasons = "; ".join(f"{c.capability}: {c.reason}" for c in rejected)
        raise EnvironmentRejected(f"环境能力不满足，拒绝执行：{reasons}")

    stages = _resolve_stages(config.stages)

    try:
        version_info = derive_version(repo_root, explicit_version=config.explicit_version)
    except VersionError as exc:
        return PipelineOutcome(None, None, None, EXIT_VERSION_ERROR, "版本推导失败", exc.reason)

    logger.info(
        "版本结论 version=%s source=%s resolution=%s",
        version_info.version, version_info.source, version_info.resolution,
    )

    if not config.specs:
        return PipelineOutcome(
            version_info,
            None,
            None,
            EXIT_EMPTY_MATRIX,
            "构建矩阵为空：没有任何目标，拒绝执行",
        )

    # 环境摘要：白名单变量；显式注入环境与进程环境在此统一
    env_map = normalize_environment(config.env)
    env_digest = digest_environment(env_map, allowed=CACHE_ENV_WHITELIST)

    # 输入源摘要：所有目标声明的输入并集；缺失即整体失败（拒绝在输入不明时复用）
    input_paths = sorted({p for spec in config.specs for p in spec.inputs})
    try:
        sources_digest = (
            digest_paths([Path(p) for p in input_paths], base=repo_root)
            if input_paths else "no-declared-inputs"
        )
    except FileNotFoundError as exc:
        raise PipelineError(str(exc)) from exc

    cache = BuildCache(work_root / "cache") if config.cache_enabled else None
    sweep_stale(work_root, prefix="build-")

    report = run_matrix(
        config.specs,
        version=version_info.version,
        repo_root=repo_root,
        work_root=work_root / "builds",
        cache=cache,
        env_digest=env_digest,
        sources_digest=sources_digest,
        max_workers=config.max_workers,
    )

    if report.conclusion is MatrixConclusion.FAILURE:
        return PipelineOutcome(
            version_info, report, None, EXIT_ALL_FAILED,
            f"全部 {len(report.failed)} 个目标构建失败：{list(report.failed)}，无可用产物",
        )
    if report.conclusion is MatrixConclusion.PARTIAL_FAILURE:
        summary = (
            f"部分失败：成功 {list(report.successful)}（产物可用），"
            f"失败 {list(report.failed)}（产物不可用）；不进入上线"
        )
        return PipelineOutcome(version_info, report, None, EXIT_PARTIAL_FAILURE, summary)

    if not config.rollout_enabled:
        return PipelineOutcome(
            version_info, report, None, EXIT_OK,
            f"全部 {len(report.successful)} 个目标构建成功（未启用上线）",
        )

    try:
        rollout = execute_rollout(
            report=report,
            version=version_info.version,
            state_dir=work_root / "rollout-state",
            env_root=work_root / "env",
            stages=stages,
        )
    except PipelineError as exc:
        return PipelineOutcome(
            version_info, report, None, EXIT_ROLLOUT_ERROR,
            f"上线/回滚失败：{exc.reason}", exc.reason,
        )

    if rollout.rolled_back:
        return PipelineOutcome(
            version_info, report, rollout, EXIT_ROLLED_BACK,
            f"上线被健康门拦截，已自动回滚并校验对外状态一致：{rollout.reason}",
            rollout.reason,
        )
    return PipelineOutcome(
        version_info, report, rollout, EXIT_OK,
        f"全部 {len(report.successful)} 个目标构建成功，并完成上线到 "
        f"{rollout.reached_stage.value if rollout.reached_stage else '-'}",
    )
