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
* per stage: the rollout is simulated from the persisted starting stage with
  the same health policy and the same deploy state-machine rules. The walk
  follows the canonical ``none -> staging -> canary -> full`` chain one
  gated step at a time until the LAST requested stage is reached, so stages
  the request skips (e.g. ``staging,full`` still gates ``canary``) are
  simulated too. A clean, superseded ``full`` restarts at ``none``; a
  foreign release mid-flight predicts ``release-in-progress``. Hence
  ``final_stage`` / ``blocked_at`` / ``block_reason`` match the real run;
* the predicted exit code uses the same precedence rules as the real run
  (all-failed 4 > partial 2 > conflict 1 > health-blocked 3 > ok 0).

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
    EXIT_ERROR,
    EXIT_OK,
    EXIT_ROLLOUT_BLOCKED,
    STAGE_RANK,
    PipelineConfig,
    _build_targets,
    _requested_destination,
)
from .state import read_state_readonly
from .validate import validate_config
from .versioning import derive_version

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
    from_stage = state.get("stage", RolloutStage.NONE.value)
    destination = _requested_destination(cfg)
    return _simulate_chain(cfg, target, state, from_stage, destination)


def _simulate_chain(
    cfg: PipelineConfig,
    target: str,
    state: dict,
    from_stage: str,
    destination: RolloutStage,
) -> StagePlan:
    """Predict one target's rollout using the exact deploy state rules.

    The walk mirrors :func:`release_pipeline.deploy.rollout` and the real
    orchestration loop, so the prediction can never disagree with the run:

    * a different version sitting at completed ``full`` starts the new
      release over at ``none`` (the real run resets state and walks again);
    * a different version mid-flight (any other persisted stage, or a
      rolled-back ``full``) refuses the promotion — ``release-in-progress``;
    * otherwise the walk advances one canonical stage at a time until the
      LAST requested stage is reached, health-gating every intermediate
      stage, including ones the request skipped (e.g. ``staging,full``
      still gates ``canary``);
    * persisted progress already beyond the requested destination means the
      real run makes zero rollout calls — nothing changes.
    """
    version = getattr(cfg, "_planned_version", None)
    stored_version = state.get("version")
    stage = from_stage

    # A foreign (different-version) release: a completed full is superseded
    # and the new release restarts at none; anything else mid-flight refuses.
    foreign = bool(state and stored_version and stored_version != version)
    if foreign:
        if stage == RolloutStage.FULL.value and not state.get("rolled_back"):
            stage = RolloutStage.NONE.value
        else:
            return StagePlan(
                target=target,
                from_stage=from_stage,
                final_stage=from_stage,
                blocked_at=from_stage,
                block_reason=(
                    f"release-in-progress: {stored_version} at {from_stage}"
                ),
            )

    if not foreign and STAGE_RANK[stage] >= STAGE_RANK[destination.value]:
        # This release is already at/beyond the requested destination; the
        # real run performs no promoting rollout calls (idempotent no-op).
        return StagePlan(
            target=target,
            from_stage=from_stage,
            final_stage=stage,
            blocked_at=None,
            block_reason="already-beyond-requested-stages",
        )

    policy = {k: tuple(v) for k, v in cfg.health_policy.items()}
    while STAGE_RANK[stage] < STAGE_RANK[destination.value]:
        nxt = NEXT_STAGE[RolloutStage(stage)].value
        healthy, conclusive = policy.get(nxt, _GOOD)
        if healthy and conclusive:
            stage = nxt
            continue
        reason = "health-inconclusive" if not conclusive else "health-failed"
        return StagePlan(
            target=target,
            from_stage=from_stage,
            final_stage=stage,
            blocked_at=nxt,
            block_reason=f"{reason} at stage {nxt}",
        )
    return StagePlan(
        target=target,
        from_stage=from_stage,
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
        would_exit = EXIT_ERROR
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
