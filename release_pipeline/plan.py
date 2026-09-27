"""Dry-run ("预演") planning: predict a run without leaving any trace.

Zero-trace guarantee
--------------------
The planner only *reads*: the repository (version derivation), the cache
(directory-free probe that never evicts), the persisted rollout state and the
environment ``current`` symlinks. It never creates directories, never writes
cache entries, never mutates state, never touches visible releases and never
writes records — not even when it fails midway, because every read helper is
side-effect-free by construction.

Consistency guarantee
---------------------
The plan is computed from exactly the same inputs and the same decision
rules as :func:`release_pipeline.pipeline.run_pipeline`:

* per target: ``reuse`` iff the cache probe hits, ``rebuild`` on
  miss/corruption, ``fail`` for targets configured to fail — matching the
  real run's ``cache-hit`` / ``built`` / ``failed`` outcomes;
* per stage: the rollout is simulated from the persisted starting stage
  along the canonical chain ``none -> staging -> canary -> full`` until it
  reaches the LAST requested stage, with the same health policy on every
  gate (including skipped intermediate stages), so ``from_stage`` /
  ``final_stage`` / ``blocked_at`` / ``block_reason`` match the real run's
  actual end stage, rollback gate and reason;
* the predicted exit code uses the same precedence rules as the real run.

When persisted progress already reached or passed the last requested stage,
both the plan and the real run are a no-op for that target
(``blocked_at`` is null). When a different release is mid-flight in the
persisted state, the plan predicts ``would_exit_code`` 1, matching the
``release_in_progress`` error the real run raises.

Concurrency
-----------
All reads are of atomically-published files (cache entries, state files and
symlinks are all installed via rename), so a plan running concurrently with
real executions observes a consistent snapshot and cannot corrupt — or be
corrupted by — shared state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .builder import (
    fingerprint_config,
    fingerprint_environment,
    hash_source_tree,
)
from .cache import compute_cache_key, probe_cache_entry
from .deploy import NEXT_STAGE
from .log import log
from .models import BuildInput, RolloutStage, VersionInfo
from .parity import check_parity
from .pipeline import (
    EXIT_BUILD_FAILED,
    EXIT_BUILD_FAILED_ALL,
    EXIT_OK,
    EXIT_ROLLOUT_BLOCKED,
    PipelineConfig,
    _build_targets,
)
from .state import read_state_readonly
from .validate import validate_config
from .versioning import derive_version

# Predicted exit code when the real run would raise a contract/state error
# (e.g. a different release is mid-flight in the persisted state).
EXIT_WOULD_ERROR = 1

_GOOD = (True, True)


@dataclass(frozen=True)
class TargetPlan:
    """Per-target prediction: reuse the cache, rebuild, or fail."""

    target: str
    action: str  # "reuse" | "rebuild" | "fail"
    cache_key: str
    cache_state: str  # "hit" | "miss" | "corrupted" | "n/a"
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "action": self.action,
            "cache_key": self.cache_key,
            "cache_state": self.cache_state,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StagePlan:
    """Per-target rollout prediction across the requested stage chain."""

    target: str
    from_stage: str
    final_stage: str
    blocked_at: str | None = None
    block_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "from_stage": self.from_stage,
            "final_stage": self.final_stage,
            "blocked_at": self.blocked_at,
            "block_reason": self.block_reason,
        }


@dataclass(frozen=True)
class PlanReport:
    """Machine-readable dry-run conclusion for one set of inputs."""

    version: VersionInfo
    targets: tuple[TargetPlan, ...]
    stages: tuple[StagePlan, ...]
    would_exit_code: int
    summary: str
    degraded: str = ""

    def to_dict(self) -> dict:
        return {
            "version": {
                "version": self.version.version,
                "commit": self.version.commit,
                "dirty": self.version.dirty,
                "commit_count": self.version.commit_count,
                "source": self.version.source,
                "reproducible_hash": self.version.reproducible_hash,
                "changelog": [
                    [sha, subject] for sha, subject in self.version.changelog
                ],
            },
            "targets": [t.to_dict() for t in self.targets],
            "stages": [s.to_dict() for s in self.stages],
            "would_exit_code": self.would_exit_code,
            "summary": self.summary,
            "degraded": self.degraded,
        }


def _plan_stages_for_target(
    cfg: PipelineConfig,
    target: str,
    state_dir: Path,
) -> StagePlan:
    """Simulate the rollout chain for one target from persisted state."""
    state = read_state_readonly(state_dir / target)
    current = state.get("stage", RolloutStage.NONE.value)
    return _simulate_chain(cfg, target, state, current, cfg.stages[-1])


def _simulate_chain(
    cfg: PipelineConfig,
    target: str,
    state: dict,
    current: str,
    target_stage: str,
) -> StagePlan:
    version = getattr(cfg, "_planned_version", None)
    stored_version = state.get("version")
    if state and stored_version and stored_version != version:
        if not (
            current == RolloutStage.FULL.value and not state.get("rolled_back")
        ):
            return StagePlan(
                target=target,
                from_stage=current,
                final_stage=current,
                blocked_at=current,
                block_reason=(
                    f"release-in-progress: {stored_version} at {current}"
                ),
            )

    order = [s.value for s in RolloutStage]  # none < staging < canary < full
    if current not in order or order.index(current) >= order.index(target_stage):
        # Persisted progress already reached or passed the last requested
        # stage; the real run performs no rollout calls in this case.
        return StagePlan(
            target=target,
            from_stage=current,
            final_stage=current,
            blocked_at=None,
            block_reason="already-beyond-requested-stages",
        )

    policy = {k: tuple(v) for k, v in cfg.health_policy.items()}
    stage = current
    while stage != target_stage:
        nxt = NEXT_STAGE[RolloutStage(stage)].value
        healthy, conclusive = policy.get(nxt, _GOOD)
        if healthy and conclusive:
            stage = nxt
            continue
        reason = "health-inconclusive" if not conclusive else "health-failed"
        return StagePlan(
            target=target,
            from_stage=current,
            final_stage=stage,
            blocked_at=nxt,
            block_reason=f"{reason} at stage {nxt}",
        )
    return StagePlan(
        target=target,
        from_stage=current,
        final_stage=stage,
        blocked_at=None,
        block_reason="",
    )


def plan_pipeline(cfg: PipelineConfig) -> PlanReport:
    """Compute the dry-run plan for ``cfg`` with zero side effects."""
    validate_config(cfg)
    parity = check_parity(
        mode=cfg.mode, env=cfg.env, given_params=cfg.extra_params
    )

    version = derive_version(cfg.repo_dir, declared=cfg.declared_version)
    env_fp = fingerprint_environment(cfg.env)
    cfg_fp = fingerprint_config(cfg.config or {})
    src_hash = hash_source_tree(cfg.repo_dir)

    work = Path(cfg.work_dir)
    cache_dir = work / "cache"
    state_dir = work / "state"

    target_plans: list[TargetPlan] = []
    for target in _build_targets(cfg):
        inp = BuildInput(
            target=target,
            version=version.version,
            source_hash=src_hash,
            env_fingerprint=env_fp,
            config_fingerprint=cfg_fp,
            toolchain="python3",
        )
        key = compute_cache_key(inp)
        if target.fail:
            target_plans.append(
                TargetPlan(
                    target=target.name,
                    action="fail",
                    cache_key=key,
                    cache_state="n/a",
                    reason="build-error: target configured to fail",
                )
            )
            continue
        probe = probe_cache_entry(cache_dir, inp)
        action = "reuse" if probe == "hit" else "rebuild"
        target_plans.append(
            TargetPlan(
                target=target.name,
                action=action,
                cache_key=key,
                cache_state=probe,
                reason="cache-hit" if probe == "hit" else f"cache-{probe}",
            )
        )

    # Deploy is only attempted for targets whose build would succeed.
    stage_plans: list[StagePlan] = []
    plan_cfg = _PlanView(cfg, version.version)
    for tp in target_plans:
        if tp.action == "fail":
            continue
        stage_plans.append(_plan_stages_for_target(plan_cfg, tp.target, state_dir))

    n_fail = sum(1 for t in target_plans if t.action == "fail")
    if n_fail == 0:
        failure_kind = "none"
    elif n_fail == len(target_plans):
        failure_kind = "all"
    else:
        failure_kind = "partial"

    would_error = any(
        s.block_reason.startswith("release-in-progress") for s in stage_plans
    )
    deploy_blocked = any(s.blocked_at for s in stage_plans) and not would_error

    if failure_kind == "all":
        would_exit = EXIT_BUILD_FAILED_ALL
    elif failure_kind == "partial":
        would_exit = EXIT_BUILD_FAILED
    elif would_error:
        would_exit = EXIT_WOULD_ERROR
    elif deploy_blocked:
        would_exit = EXIT_ROLLOUT_BLOCKED
    else:
        would_exit = EXIT_OK

    summary = " ".join(
        [
            f"version={version.version}",
            f"reuse={sum(1 for t in target_plans if t.action == 'reuse')}",
            f"rebuild={sum(1 for t in target_plans if t.action == 'rebuild')}",
            f"fail={n_fail}",
            f"failure_kind={failure_kind}",
            f"blocked={[f'{s.target}@{s.blocked_at}' for s in stage_plans if s.blocked_at]}",
            f"would_exit={would_exit}",
        ]
    )
    log("plan", summary)
    return PlanReport(
        version=version,
        targets=tuple(target_plans),
        stages=tuple(stage_plans),
        would_exit_code=would_exit,
        summary=summary,
        degraded=parity.get("degraded", ""),
    )


class _PlanView:
    """Read-only view of the config carrying the planned version.

    Lets the stage simulator read the would-be release version without
    mutating the caller's :class:`PipelineConfig`.
    """

    def __init__(self, cfg: PipelineConfig, version: str) -> None:
        self._cfg = cfg
        self._planned_version = version

    def __getattr__(self, name: str):  # delegate everything else
        return getattr(self._cfg, name)
