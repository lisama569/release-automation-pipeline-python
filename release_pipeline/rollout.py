"""分阶段上线与自动回滚：canary -> partial -> full，状态原子推进。

目标分组（4 个模拟目标时）：
- canary:  1 个（canary）
- partial: 2 个（partial-a/b）
- full:    全部（canary, partial-a/b, full）

每个阶段：stage 产物 -> promote（原子切指针）-> 逐目标健康探针。
- 全部健康才进入下一阶段；
- 任一目标不健康或结论不明确 => 立即停止推进，全部已推进目标原子回滚到上线前版本，
  并校验所有目标对外状态与上线前完全一致，否则报 ROLLBACK_INCONSISTENT；
- 无产物的构建失败目标：永远不进入发布，状态停留在旧版本，不影响回滚语义。

中断重试：<state_dir>/rollout-state.json 记录每个目标的上线前版本、已完成阶段、
当前版本。文件锁 + 原子写保证并发/中断不产生半更新。已完成阶段幂等跳过。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .builder import MatrixReport
from .errors import RolloutError
from .locking import FileLock, read_json, write_json_atomic
from .models import (
    HealthProbe,
    ReleaseStage,
    ReleaseState,
    RolloutReport,
    StageResult,
)
from .targets import LocalTargetEnvironment
from .health import probe_target

logger = logging.getLogger(__name__)

STAGE_PLAN: dict[ReleaseStage, list[str]] = {
    ReleaseStage.CANARY: ["canary"],
    ReleaseStage.PARTIAL: ["canary", "partial-a", "partial-b"],
    ReleaseStage.FULL: ["canary", "partial-a", "partial-b", "full"],
}
STAGE_ORDER: tuple[ReleaseStage, ...] = (
    ReleaseStage.CANARY,
    ReleaseStage.PARTIAL,
    ReleaseStage.FULL,
)


def open_target(env_root: Path, target: str) -> LocalTargetEnvironment:
    return LocalTargetEnvironment(Path(env_root), target)


def execute_rollout(
    *,
    report: MatrixReport,
    version: str,
    state_dir: Path,
    env_root: Path,
    stages: list[ReleaseStage] | None = None,
) -> RolloutReport:
    """按阶段推进上线；失败自动回滚。可中断重试，已完成阶段不重复推进。"""
    state_dir = Path(state_dir)
    env_root = Path(env_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    plan_stages = stages or list(STAGE_ORDER)

    artifacts = {r.target: Path(r.artifact_path) for r in report.results
                 if r.status.value == "success" and r.artifact_path}
    all_targets = _targets_for_plan(plan_stages)
    missing = [t for t in all_targets if t not in artifacts]
    if missing:
        raise RolloutError(
            f"无法上线：阶段计划需要但构建矩阵未成功产出的目标 {missing}",
            details={"missing": missing, "version": version},
        )

    state_path = state_dir / "rollout-state.json"
    lock_path = state_dir / "rollout.lock"
    with FileLock(lock_path, timeout=30.0):
        state = _load_state(state_path)
        prior_version = state.get("version")
        _guard_state_version(state, version)
        starting_new_round = prior_version is not None and prior_version != version

        before = _snapshot_before(state if not starting_new_round else {},
                                  all_targets, env_root)
        if starting_new_round:
            # 新版本 = 新一轮：重置轮次级推进记录；上线前快照以当前对外状态为准
            state = {
                "version": version,
                "before": before,
                "stages": {},
                "staged": {},
                "promoted": {},
            }
            logger.info(
                "rollout 开启新版本轮次 %s（上一版本 %s），上线前快照=%s",
                version, prior_version, before,
            )
        else:
            state["version"] = version
            state["before"] = before
            state["stages"] = state.get("stages", {})
        _save_state(state_path, state)

        stage_results: list[StageResult] = []
        reached: ReleaseStage | None = None

        for stage in plan_stages:
            targets = STAGE_PLAN[stage]
            stage_key = stage.value
            prior = state["stages"].get(stage_key)

            if prior == "done":
                logger.info("rollout stage=%s 已完成，重试幂等跳过", stage_key)
                probes = tuple(
                    probe_target(env_root, target=t, version=version) for t in targets
                )
                stage_results.append(
                    StageResult(stage=stage, state=ReleaseState.DEPLOYED,
                                probes=probes, rolled_back=False)
                )
                reached = stage
                continue

            # 清理上次中断遗留暂存，再暂存并推进
            probes, gate_failure = _advance_stage(
                stage, targets, version, artifacts, env_root, state, state_path
            )
            reached = stage

            if gate_failure is not None:
                logger.warning(
                    "rollout stage=%s 健康门未通过，触发自动回滚: %s",
                    stage_key, gate_failure,
                )
                _rollback_all(before, env_root, all_targets)
                state["stages"][stage_key] = "rolled_back"
                state["final_state"] = ReleaseState.ROLLED_BACK.value
                _save_state(state_path, state)
                stage_results.append(
                    StageResult(stage=stage, state=ReleaseState.ROLLED_BACK,
                                probes=probes, rolled_back=True)
                )
                return RolloutReport(
                    version=version,
                    reached_stage=stage,
                    final_state=ReleaseState.ROLLED_BACK,
                    rolled_back=True,
                    stages=tuple(stage_results),
                    reason=gate_failure,
                )

            state["stages"][stage_key] = "done"
            state["final_state"] = ReleaseState.DEPLOYED.value
            _save_state(state_path, state)
            stage_results.append(
                StageResult(stage=stage, state=ReleaseState.DEPLOYED,
                            probes=probes, rolled_back=False)
            )

        return RolloutReport(
            version=version,
            reached_stage=reached,
            final_state=ReleaseState.DEPLOYED,
            rolled_back=False,
            stages=tuple(stage_results),
            reason=None,
        )


def _targets_for_plan(plan_stages: list[ReleaseStage]) -> list[str]:
    result: list[str] = []
    for stage in plan_stages:
        for t in STAGE_PLAN[stage]:
            if t not in result:
                result.append(t)
    return result


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        raise RolloutError("rollout-state.json 结构损坏，拒绝在不明状态下继续上线")
    return data


def _save_state(path: Path, state: dict) -> None:
    write_json_atomic(path, state)


def _guard_state_version(state: dict, version: str) -> None:
    """判定是否允许在现有发布状态上推进 ``version``。

    - 无历史状态：允许（全新发布）。
    - 上一轮已处于终态（deployed / rolled_back）：允许新版本发布——其上线前
      快照就是当前对外版本，这正是常规升级路径。
    - 同版本重试：允许（幂等恢复）。
    - 上一轮仍在进行中（存在未完成且未回滚的阶段）却换版本：拒绝，避免两个
      版本在同一环境交错推进留下半状态。
    """
    existing = state.get("version")
    if existing is None or existing == version:
        return
    final_state = state.get("final_state")
    if final_state in (ReleaseState.DEPLOYED.value, ReleaseState.ROLLED_BACK.value):
        logger.info(
            "rollout 上一版本 %s 已处于终态 %s，允许升级到 %s",
            existing, final_state, version,
        )
        return
    raise RolloutError(
        f"版本 {existing} 的发布仍在进行中（状态={final_state}），"
        f"此时请求推进 {version}：拒绝混用发布状态（请先完成或回滚上一轮）",
        details={"state_version": existing, "requested": version,
                 "final_state": final_state},
    )


def _snapshot_before(state: dict, targets: list[str], env_root: Path) -> dict[str, str | None]:
    if "before" in state:
        return {t: state["before"].get(t) for t in targets}
    before: dict[str, str | None] = {}
    for t in targets:
        env = open_target(env_root, t)
        env.clear_staged()
        before[t] = env.current_version
    logger.info("rollout 上线前对外状态快照: %s", before)
    return before


def _advance_stage(
    stage: ReleaseStage,
    targets: list[str],
    version: str,
    artifacts: dict[str, Path],
    env_root: Path,
    state: dict,
    state_path: Path,
) -> tuple[tuple[HealthProbe, ...], str | None]:
    # 1) 全部暂存（不改变对外状态）；任一失败 => 不推进任何指针
    for t in targets:
        env = open_target(env_root, t)
        env.clear_staged()
        env.stage(version, artifacts[t])
    state.setdefault("staged", {})[stage.value] = targets
    _save_state(state_path, state)

    # 2) 原子 promote（指针切换要么发生要么不发生）
    for t in targets:
        open_target(env_root, t).promote(version)
    state.setdefault("promoted", {})[stage.value] = targets
    _save_state(state_path, state)

    # 3) 健康探针：任一不明确/不健康即拒绝推进
    probes: list[HealthProbe] = []
    for t in targets:
        probe = probe_target(env_root, target=t, version=version)
        probes.append(probe)
        if probe.ambiguous:
            return tuple(probes), f"阶段 {stage.value} 目标 {t} 健康结论不明确：{probe.detail}"
        if not probe.healthy:
            return tuple(probes), f"阶段 {stage.value} 目标 {t} 健康检查失败：{probe.detail}"
    return tuple(probes), None


def _rollback_all(
    before: dict[str, str | None], env_root: Path, targets: list[str]
) -> None:
    """把所有目标的 current 原子切回上线前版本（或移除指针），并校验一致性。"""
    for t in targets:
        env = open_target(env_root, t)
        prior = before.get(t)
        if prior is None:
            # 上线前无对外版本：原子移除 current 指针，恢复“无版本”状态
            link = env.root / "current"
            if link.is_symlink():
                link.unlink()
        else:
            env.rollback_to(prior)

    if not verify_external_state(env_root, targets=targets, expected_map=before):
        raise RolloutError(
            "回滚后对外状态与上线前不一致（ROLLBACK_INCONSISTENT）",
            details={"expected": before},
        )
    logger.info("rollout 自动回滚完成，对外状态已恢复为上线前快照: %s", before)


def verify_external_state(
    env_root: Path,
    *,
    targets: list[str],
    expected_version: str | None = None,
    expected_map: dict[str, str | None] | None = None,
) -> bool:
    """校验每个目标对外可见版本是否与期望完全一致。"""
    ok = True
    for t in targets:
        actual = open_target(env_root, t).current_version
        expected = expected_map[t] if expected_map is not None else expected_version
        match = actual == expected
        ok = ok and match
        logger.info(
            "verify target=%s 对外版本=%s 期望=%s 判定=%s",
            t, actual, expected, "一致" if match else "不一致",
        )
    return ok
