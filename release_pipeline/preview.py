"""Dry-run (preview) of a pipeline execution.

Zero-trace guarantee
--------------------
``preview_pipeline`` performs *no* writes at all: it creates no directories,
no cache entries, no artifacts, no release dirs, no state files and no
records. Every conclusion is derived from read-only probes of the same
inputs the real execution would use, and the planning logic is shared with
the real pipeline, so preview and execution agree item by item.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .builder import (
    fingerprint_config,
    fingerprint_environment,
    hash_source_tree,
    planned_content_hash,
)
from .cache import PROBE_HIT, compute_cache_key, probe_entry, read_meta_content_hash
from .contract import DEFAULT_KNOWN_TARGETS, validate_request
from .models import BuildInput, RolloutStage
from .parity import check_parity
from .pipeline import (
    PipelineConfig,
    classify_outcome,
    compute_exit_code,
)
from .planner import plan_rollout
from .state import read_state_readonly
from .versioning import derive_version


@dataclass(frozen=True)
class TargetPreview:
    """Per-target build preview."""

    target: str
    action: str  # "reuse" | "rebuild" | "fail"
    reason: str
    cache_key: str
    content_hash: str


@dataclass(frozen=True)
class DeployPreview:
    """Per-target rollout preview."""

    target: str
    current_stage: str
    planned_stages: tuple[str, ...]
    final_stage: str
    blocked_at: str
    blocked_reason: str


@dataclass(frozen=True)
class PreviewResult:
    """Machine-readable dry-run conclusion for one request."""

    version: str
    reproducible_hash: str
    changelog: tuple[tuple[str, str], ...]
    builds: tuple[TargetPreview, ...]
    deploys: tuple[DeployPreview, ...]
    predicted_exit_code: int
    outcome: str
    summary: str


def preview_pipeline(cfg: PipelineConfig) -> PreviewResult:
    """Compute the dry-run conclusion for ``cfg`` without any writes.

    Zero-trace: this function performs no directory creation, no cache
    mutation, no state writes and no record writes. Every conclusion is
    derived from the same read-only inputs and the same pure planning
    functions the real execution uses, so the two agree item by item.
    Contract violations raise before any probing happens.
    """
    # Same contracts, same order as run_pipeline — and still no writes.
    validate_request(
        targets=cfg.targets,
        stages=cfg.stages,
        failing_targets=cfg.failing_targets,
        known_targets=cfg.known_targets or DEFAULT_KNOWN_TARGETS,
    )
    check_parity(mode=cfg.mode, env=cfg.env, given_params=cfg.extra_params)

    work = Path(cfg.work_dir)
    cache_dir = work / "cache"
    state_dir = work / "state"

    version = derive_version(cfg.repo_dir, declared=cfg.declared_version)
    env_fp = fingerprint_environment(cfg.env)
    cfg_fp = fingerprint_config(cfg.config or {})
    src_hash = hash_source_tree(cfg.repo_dir)

    builds: list[TargetPreview] = []
    deploys: list[DeployPreview] = []
    n_failed = 0
    n_succeeded = 0
    deploy_blocked = False
    for i, name in enumerate(cfg.targets):
        from .models import BuildTarget

        target = BuildTarget(
            name=name,
            artifact_name=f"app-{i}-{name}.bin",
            fail=name in cfg.failing_targets,
        )
        inp = BuildInput(
            target=target,
            version=version.version,
            source_hash=src_hash,
            env_fingerprint=env_fp,
            config_fingerprint=cfg_fp,
            toolchain="python3",
        )
        cache_key = compute_cache_key(inp)
        if target.fail:
            action, reason, content_hash = (
                "fail",
                "build-error: target configured to fail",
                "",
            )
            n_failed += 1
        else:
            probe = probe_entry(cache_dir, inp)
            if probe == PROBE_HIT:
                action = "reuse"
                reason = "cache-hit"
                content_hash = read_meta_content_hash(cache_dir, inp)
                n_succeeded += 1
            else:
                # miss or corrupt: the real run would (re)build either way.
                action = "rebuild"
                reason = f"cache-{probe}"
                content_hash = planned_content_hash(target, version)
                n_succeeded += 1
        builds.append(
            TargetPreview(
                target=name,
                action=action,
                reason=reason,
                cache_key=cache_key,
                content_hash=content_hash,
            )
        )

        if action == "fail":
            # No artifact -> the real pipeline skips deployment entirely.
            continue
        state = read_state_readonly(state_dir / name)
        current = RolloutStage(state.get("stage", RolloutStage.NONE.value))
        plan = plan_rollout(current, cfg.stages, dict(cfg.health_policy))
        if plan.blocked:
            deploy_blocked = True
        deploys.append(
            DeployPreview(
                target=name,
                current_stage=current.value,
                planned_stages=tuple(s.to_stage for s in plan.steps),
                final_stage=plan.final_stage,
                blocked_at=plan.blocked_at,
                blocked_reason=(
                    plan.steps[-1].blocked_reason
                    if plan.blocked and plan.steps
                    else ""
                ),
            )
        )

    exit_code = compute_exit_code(n_failed=n_failed, deploy_blocked=deploy_blocked)
    outcome = classify_outcome(
        n_succeeded=n_succeeded,
        n_failed=n_failed,
        deploy_blocked=deploy_blocked,
    )
    summary = (
        f"preview version={version.version} "
        f"builds={n_succeeded}/{len(cfg.targets)} "
        f"actions={[(b.target, b.action) for b in builds]} "
        f"blocked={[d.target + '@' + d.blocked_at for d in deploys if d.blocked_at]} "
        f"outcome={outcome} exit={exit_code}"
    )
    return PreviewResult(
        version=version.version,
        reproducible_hash=version.reproducible_hash,
        changelog=version.changelog,
        builds=tuple(builds),
        deploys=tuple(deploys),
        predicted_exit_code=exit_code,
        outcome=outcome,
        summary=summary,
    )
