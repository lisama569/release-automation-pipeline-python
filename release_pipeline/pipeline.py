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
2  some (but not all) build targets failed (others may still be deployed)
3  a rollout stage was blocked and rolled back
4  every requested build target failed
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
from .deploy import SimulatedEnvironment, rollout, visible_version_readonly
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
from .record import build_record, write_record
from .state import ReleaseStateStore, read_state_readonly
from .validate import validate_config
from .versioning import derive_version

EXIT_OK = 0
EXIT_BUILD_FAILED = 2
EXIT_ROLLOUT_BLOCKED = 3
EXIT_BUILD_FAILED_ALL = 4


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


@dataclass
class PipelineResult:
    version: VersionInfo
    build: BuildReport
    deploys: tuple[DeployResult, ...]
    exit_code: int
    summary: str
    degraded: str = ""
    # "none" | "partial" | "all": keeps total build failure machine-
    # distinguishable from a partial one instead of merging both into
    # a single "failed" conclusion.
    failure_kind: str = "none"
    # Machine-readable, byte-deterministic receipt of this real run.
    record: dict = field(default_factory=dict)


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

    The function never raises for expected build/deploy failures; they are
    encoded in the result and the exit code. Contract-violating requests
    (empty targets, unknown targets/stages, non-monotonic stage sequences)
    are rejected by :func:`validate_config` *before* any side effect.
    """
    validate_config(cfg)
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
    for result in report.results:
        target_name = result.target
        if result.artifact is None:
            log(
                "pipeline",
                "skip deploy: no usable artifact",
                target=target_name,
                reason=result.reason,
            )
            continue
        store = ReleaseStateStore(state_dir / target_name)
        env = SimulatedEnvironment(
            env_root / target_name,
            health_by_stage=dict(cfg.health_policy),
        )
        # Advance through exactly the configured number of stages.
        chain = [RolloutStage.NONE] + [RolloutStage(s) for s in cfg.stages]
        current = _current_stage(store)
        while current in chain and current != chain[-1]:
            dr = rollout(env, result.artifact, version.version, store)
            deploys.append(dr)
            if dr.rolled_back:
                deploy_blocked = True
                break
            current = dr.stage

    # Distinguish "every target failed" from "some targets failed"; the two
    # must never collapse into one conclusion.
    if not report.failed:
        failure_kind = "none"
    elif len(report.failed) == len(report.results):
        failure_kind = "all"
    else:
        failure_kind = "partial"

    exit_code = EXIT_OK
    if failure_kind == "all":
        exit_code = EXIT_BUILD_FAILED_ALL
    elif failure_kind == "partial":
        exit_code = EXIT_BUILD_FAILED
    if deploy_blocked and exit_code == EXIT_OK:
        exit_code = EXIT_ROLLOUT_BLOCKED
    # Partial/total build failure and rollout failure are both reportable;
    # build failure keeps its dedicated code, rollout is in the summary.

    parts = [
        f"version={version.version}",
        f"builds={len(report.succeeded)}/{len(report.results)}",
        f"failure_kind={failure_kind}",
        f"failed_targets={[r.target for r in report.failed]}",
        f"rollbacks={[d.version + '@' + d.stage.value for d in deploys if d.rolled_back]}",
        f"exit={exit_code}",
    ]
    summary = " ".join(parts)
    log("pipeline", f"{summary}")

    # Collect the final per-target stage state via read-only probes (no
    # directories, locks or symlinks are created by these reads), then emit
    # the deterministic release record for this real execution.
    final_states: list[dict] = []
    for name in cfg.targets:
        state = read_state_readonly(state_dir / name)
        visible = visible_version_readonly(env_root / name)
        final_states.append(
            {
                "target": name,
                "final_stage": state.get("stage", RolloutStage.NONE.value),
                "rolled_back": bool(state.get("rolled_back", False)),
                "visible_version": visible,
            }
        )
    record = build_record(
        version=version,
        targets=cfg.targets,
        stages=cfg.stages,
        failing_targets=cfg.failing_targets,
        health_policy=cfg.health_policy,
        env_fingerprint=env_fp,
        config_fingerprint=cfg_fp,
        source_hash=src_hash,
        build=report,
        final_states=tuple(final_states),
        exit_code=exit_code,
        failure_kind=failure_kind,
        deploy_blocked=deploy_blocked,
    )
    write_record(record, work / "records")
    log("pipeline", "release record written", targets=len(cfg.targets))

    return PipelineResult(
        version=version,
        build=report,
        deploys=tuple(deploys),
        exit_code=exit_code,
        summary=summary,
        degraded=parity.get("degraded", ""),
        failure_kind=failure_kind,
        record=record,
    )


def _current_stage(store: ReleaseStateStore) -> RolloutStage:
    state = store.read()
    if not state:
        return RolloutStage.NONE
    return RolloutStage(state.get("stage", RolloutStage.NONE.value))
