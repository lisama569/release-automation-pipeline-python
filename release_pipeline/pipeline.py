"""End-to-end orchestration: version -> build matrix -> staged rollout.

Determinism
-----------
The orchestration layer adds no nondeterminism: no wall-clock fields, and
every external influence (environment, config, health policy) is passed
explicitly via :class:`PipelineConfig`. Running the same config twice in
``local`` mode and twice in ``ci`` mode yields identical versions, identical
cache decisions and identical verdicts.

Exit codes
----------
0  all targets built and every configured stage advanced
2  one or more build targets failed (other targets may still be deployed)
3  a rollout stage was blocked and rolled back
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .builder import (
    fingerprint_config,
    fingerprint_environment,
    hash_source_tree,
    run_matrix,
)
from .cache import BuildCache
from .contract import DEFAULT_KNOWN_TARGETS, validate_request
from .deploy import SimulatedEnvironment, rollout
from .models import (
    BuildReport,
    BuildTarget,
    DeployResult,
    HealthVerdict,
    RolloutStage,
    VersionInfo,
)
from .log import log
from .parity import check_parity
from .state import ReleaseStateStore
from .versioning import derive_version

EXIT_OK = 0
EXIT_BUILD_FAILED = 2
EXIT_ROLLOUT_BLOCKED = 3

OUTCOME_ALL_SUCCEEDED = "all-succeeded"
OUTCOME_PARTIAL_FAILURE = "partial-failure"
OUTCOME_ALL_FAILED = "all-failed"
ROLLOUT_BLOCKED_SUFFIX = "+rollout-blocked"


@dataclass(frozen=True)
class TargetOutcome:
    """One target's final build + rollout outcome (used by records)."""

    target: str
    build_status: str          # success | failed | skipped
    build_reason: str          # built | cache-hit | build-error: ...
    artifact_hash: str         # content hash when successful, else ""
    final_stage: str           # last stage reached; "" when not deployed
    rollout_blocked: bool


@dataclass
class PipelineConfig:
    repo_dir: str = "."
    work_dir: str = ".release-work"
    targets: tuple[str, ...] = ("linux-x86_64", "linux-aarch64")
    failing_targets: frozenset[str] = field(default_factory=frozenset)
    declared_version: str | None = None
    env: dict[str, str] | None = None
    config: dict[str, object] | None = None
    mode: str = "local"
    stages: tuple[str, ...] = ("staging", "canary", "full")
    # stage name -> (healthy, conclusive); absent stages default to healthy.
    health_policy: dict[str, tuple[bool, bool]] = field(default_factory=dict)
    extra_params: frozenset[str] = field(default_factory=frozenset)
    known_targets: tuple[str, ...] | None = None
    # When False (default), a release record is persisted after the run.
    no_record: bool = False


@dataclass
class PipelineResult:
    version: VersionInfo
    build: BuildReport
    deploys: tuple[DeployResult, ...]
    exit_code: int
    summary: str
    degraded: str = ""
    target_outcomes: tuple[TargetOutcome, ...] = ()
    record_id: str = ""
    record_path: str = ""
    outcome: str = OUTCOME_ALL_SUCCEEDED
    # Explicit request inputs (fingerprints, never secrets or paths): the
    # only things that may influence a verdict. Consumed by release records
    # so two records can be compared input-by-input.
    request_inputs: dict = field(default_factory=dict)


def classify_outcome(
    *, n_succeeded: int, n_failed: int, deploy_blocked: bool
) -> str:
    """Deterministic, fully distinguishable overall outcome.

    All-target failure and partial failure are never merged; a rollout block
    is reported as an explicit suffix on top of the build outcome.
    """
    if n_succeeded == 0 and n_failed > 0:
        base = OUTCOME_ALL_FAILED
    elif n_failed > 0:
        base = OUTCOME_PARTIAL_FAILURE
    else:
        base = OUTCOME_ALL_SUCCEEDED
    return base + ROLLOUT_BLOCKED_SUFFIX if deploy_blocked else base


def compute_exit_code(
    *, n_failed: int, deploy_blocked: bool
) -> int:
    """Shared exit-code rule for execution and preview."""
    if n_failed:
        # Build failure keeps its dedicated code even when a rollout also
        # failed; the rollout remains visible in the outcome and the summary.
        return EXIT_BUILD_FAILED
    if deploy_blocked:
        return EXIT_ROLLOUT_BLOCKED
    return EXIT_OK


def _build_targets(cfg: PipelineConfig) -> list[BuildTarget]:
    targets: list[BuildTarget] = []
    for i, name in enumerate(cfg.targets):
        targets.append(
            BuildTarget(
                name=name,
                artifact_name=f"app-{i}-{name}.bin",
                fail=name in cfg.failing_targets,
            )
        )
    return targets


def run_pipeline(cfg: PipelineConfig) -> PipelineResult:
    """Execute the full pipeline deterministically and return a result.

    The request contract is validated and the parity contract checked
    *before* any directory is created, so an invalid request leaves no
    trace. The function never raises for expected build/deploy failures;
    they are encoded in the result and the exit code.
    """
    # 1) Contracts first — no filesystem effects may precede rejection.
    validate_request(
        targets=cfg.targets,
        stages=cfg.stages,
        failing_targets=cfg.failing_targets,
        known_targets=cfg.known_targets or DEFAULT_KNOWN_TARGETS,
    )
    parity = check_parity(
        mode=cfg.mode,
        env=cfg.env,
        given_params=cfg.extra_params,
    )

    work = Path(cfg.work_dir)
    cache_dir = work / "cache"
    artifacts_root = work / "artifacts"
    state_dir = work / "state"
    env_root = work / "environments"
    for p in (cache_dir, artifacts_root, state_dir, env_root):
        p.mkdir(parents=True, exist_ok=True)

    version = derive_version(cfg.repo_dir, declared=cfg.declared_version)
    log(
        "pipeline",
        "derived version",
        version=version.version,
        source=version.source,
        dirty=version.dirty,
        hash=version.reproducible_hash,
    )

    cache = BuildCache(cache_dir)
    env_fp = fingerprint_environment(cfg.env)
    cfg_fp = fingerprint_config(cfg.config or {})
    src_hash = hash_source_tree(cfg.repo_dir)
    log(
        "pipeline",
        "fingerprints",
        src=src_hash[:12],
        env=env_fp[:12],
        cfg=cfg_fp[:12],
    )

    report = run_matrix(
        _build_targets(cfg),
        version,
        cache,
        str(artifacts_root),
        env_fingerprint=env_fp,
        config_fingerprint=cfg_fp,
        source_hash=src_hash,
    )

    # Only successful, verified artifacts may be deployed. Each target has
    # its own simulated environment and its own state file.
    deploys: list[DeployResult] = []
    deploy_blocked = False
    target_outcomes: dict[str, TargetOutcome] = {}
    for result in report.results:
        target_name = result.target
        if result.artifact is None:
            log(
                "pipeline",
                "skip deploy: no usable artifact",
                target=target_name,
                reason=result.reason,
            )
            target_outcomes[target_name] = TargetOutcome(
                target=target_name,
                build_status="failed",
                build_reason=result.reason,
                artifact_hash="",
                final_stage="",
                rollout_blocked=False,
            )
            continue
        store = ReleaseStateStore(state_dir / target_name)
        env = SimulatedEnvironment(
            env_root / target_name,
            health_by_stage=dict(cfg.health_policy),
        )
        target_blocked = False
        # Advance through exactly the configured number of stages.
        chain = [RolloutStage.NONE] + [RolloutStage(s) for s in cfg.stages]
        current = _current_stage(store)
        while current in chain and current != chain[-1]:
            dr = rollout(env, result.artifact, version.version, store)
            deploys.append(dr)
            if dr.rolled_back:
                deploy_blocked = True
                target_blocked = True
                break
            current = dr.stage
        # A target whose state is already past the configured chain's end
        # (e.g. already full) deploys nothing and simply stays where it is.
        target_outcomes[target_name] = TargetOutcome(
            target=target_name,
            build_status="success",
            build_reason=result.reason,
            artifact_hash=result.artifact.content_hash,
            final_stage=current.value,
            rollout_blocked=target_blocked,
        )

    n_failed = len(report.failed)
    exit_code = compute_exit_code(
        n_failed=n_failed, deploy_blocked=deploy_blocked
    )
    outcome = classify_outcome(
        n_succeeded=len(report.succeeded),
        n_failed=n_failed,
        deploy_blocked=deploy_blocked,
    )

    parts = [
        f"version={version.version}",
        f"builds={len(report.succeeded)}/{len(report.results)}",
        f"failed_targets={[r.target for r in report.failed]}",
        f"rollbacks={[d.version + '@' + d.stage.value for d in deploys if d.rolled_back]}",
        f"outcome={outcome}",
        f"exit={exit_code}",
    ]
    summary = " ".join(parts)
    log("pipeline", f"{summary}")
    pipeline_result = PipelineResult(
        version=version,
        build=report,
        deploys=tuple(deploys),
        exit_code=exit_code,
        summary=summary,
        degraded=parity.get("degraded", ""),
        target_outcomes=tuple(
            target_outcomes[r.target] for r in report.results
        ),
        outcome=outcome,
        request_inputs={
            "targets": list(cfg.targets),
            "stages": list(cfg.stages),
            "source_hash": src_hash,
            "env_fingerprint": env_fp,
            "config_fingerprint": cfg_fp,
            "declared_version": cfg.declared_version,
            "health_policy": {
                k: list(v) for k, v in sorted(cfg.health_policy.items())
            },
            "failing_targets": sorted(cfg.failing_targets),
        },
    )

    # 2) Real executions always persist a durable release record.
    if not cfg.no_record:
        from .record import build_record, write_record

        rec = build_record(pipeline_result)
        rec_path = write_record(rec, work / "records")
        pipeline_result.record_id = rec["id"]
        pipeline_result.record_path = str(rec_path)
        log("pipeline", "release record written", path=str(rec_path))
    return pipeline_result


def _current_stage(store: ReleaseStateStore) -> RolloutStage:
    state = store.read()
    if not state:
        return RolloutStage.NONE
    return RolloutStage(state.get("stage", RolloutStage.NONE.value))
