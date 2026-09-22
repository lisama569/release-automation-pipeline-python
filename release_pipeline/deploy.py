"""Staged rollout against locally simulated target environments.

Safety model
------------
* Each release is materialized under ``releases/<version>/`` *before* it can
  become visible. Making a release visible is a single atomic symlink swap
  (``current`` -> release dir), so observers either see the old release or
  the new one, never a half-copied tree.
* A stage advances only when the health verdict is both **conclusive** and
  **healthy**. Inconclusive health is treated as failure (no guessing).
* On any non-advancing verdict the symlink is swapped back to exactly what
  was visible before the attempt; when nothing was live, ``current`` is
  removed. Post-rollback visibility is asserted equal to pre-deploy
  visibility and a :class:`RollbackError` is raised if it is not.
* The whole attempt runs inside a locked state transaction, so concurrent
  rollouts cannot interleave or promote twice.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from .errors import RollbackError, StateError
from .log import log
from .models import (
    Artifact,
    DeployResult,
    HealthVerdict,
    RolloutStage,
)
from .state import ReleaseStateStore, new_state

NEXT_STAGE: dict[RolloutStage, RolloutStage] = {
    RolloutStage.NONE: RolloutStage.STAGING,
    RolloutStage.STAGING: RolloutStage.CANARY,
    RolloutStage.CANARY: RolloutStage.FULL,
}

# (healthy, conclusive) defaults for a well-behaved environment.
_GOOD = (True, True)


class SimulatedEnvironment:
    """A local, deterministic deployment target on disk."""

    def __init__(
        self,
        root: str | Path,
        *,
        health_by_stage: dict[str, tuple[bool, bool]] | None = None,
        default_health: tuple[bool, bool] = _GOOD,
    ) -> None:
        self.root = Path(root)
        self.releases_dir = self.root / "releases"
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        self.current_link = self.root / "current"
        self._policy = {k: tuple(v) for k, v in (health_by_stage or {}).items()}
        self._default = tuple(default_health)
        self._sweep_crash_leftovers()

    def _sweep_crash_leftovers(self) -> None:
        """Remove temp resources an interrupted prior run could leave.

        Completed work lives in ``releases/<version>`` and the ``current``
        symlink; only ``.staging-*`` and ``.current.tmp-*`` names are
        transient and safe to delete.
        """
        for child in self.releases_dir.glob(".staging-*"):
            shutil.rmtree(child, ignore_errors=True)
        for child in self.root.glob(".current.tmp-*"):
            child.unlink(missing_ok=True)

    # ------------------------------------------------------------- inspection
    @property
    def visible_version(self) -> str | None:
        """Version currently visible externally; ``None`` if nothing live."""
        if not self.current_link.is_symlink() and not self.current_link.exists():
            return None
        try:
            target = os.readlink(self.current_link)
        except OSError:
            return None
        # Symlink target is like ``releases/<version>``.
        name = os.path.basename(target.rstrip("/"))
        return name or None

    # ------------------------------------------------------------- mutations
    def install(self, artifact: Artifact, version: str) -> Path:
        """Materialize a release directory; does NOT change visibility."""
        dest = self.releases_dir / version
        if dest.is_dir():
            # Identical re-install: verify content, keep the existing dir.
            data = Path(artifact.path).read_bytes()
            stored = dest / "artifact.bin"
            if stored.is_file() and stored.read_bytes() == data:
                return dest
            raise StateError(
                f"release {version} already exists with different content",
                code="release_content_conflict",
            )
        staging = self.releases_dir / f".staging-{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=True)
        try:
            data = Path(artifact.path).read_bytes()
            out = staging / "artifact.bin"
            with open(out, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(staging, dest)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return dest

    def _point_to(self, version: str | None) -> None:
        """Atomically set ``current``; ``None`` removes visibility."""
        if version is None:
            self.current_link.unlink(missing_ok=True)
            return
        tmp = self.root / f".current.tmp-{uuid.uuid4().hex[:8]}"
        os.symlink(os.path.join("releases", version), tmp)
        os.replace(tmp, self.current_link)

    def activate(self, version: str) -> str | None:
        """Swap visibility to ``version`` and return the previously visible one."""
        previous = self.visible_version
        self._point_to(version)
        return previous

    def activate_previous(self, previous_version: str | None) -> None:
        """Restore exact pre-attempt visibility."""
        self._point_to(previous_version)

    # ---------------------------------------------------------------- health
    def check_health(self, stage: RolloutStage) -> HealthVerdict:
        if self.visible_version is None:
            # Nothing live yet -> cannot be healthy, and that IS conclusive.
            return HealthVerdict(
                healthy=False,
                conclusive=True,
                detail="no release visible",
            )
        healthy, conclusive = self._policy.get(stage.value, self._default)
        if not conclusive:
            detail = f"health inconclusive at stage {stage.value}"
        elif healthy:
            detail = f"health checks passed at stage {stage.value}"
        else:
            detail = f"health checks FAILED at stage {stage.value}"
        return HealthVerdict(
            healthy=healthy, conclusive=conclusive, detail=detail
        )


def rollout(
    env: SimulatedEnvironment,
    artifact: Artifact,
    version: str,
    store: ReleaseStateStore,
) -> DeployResult:
    """Attempt to advance one gated stage, rolling back on any bad verdict.

    Never raises for health failure/inconclusiveness: returns a
    :class:`DeployResult` describing the rollback. Raises only for state
    conflicts or failure to actually restore the previous release.
    """
    with store.transaction() as state:
        if not state:
            state.update(new_state(version))
        elif state.get("version") != version:
            # A different release mid-flight must not be promoted by this call.
            if state.get("stage") == RolloutStage.FULL.value and not state.get(
                "rolled_back"
            ):
                # Previous release finished cleanly; start the new one on top.
                prior = state["version"]
                state.update(new_state(version))
                state["previous_version"] = prior
            else:
                raise StateError(
                    f"release {state.get('version')!s} in progress at stage "
                    f"{state.get('stage')!s}; refusing to promote {version}",
                    code="release_in_progress",
                )

        current_stage = RolloutStage(state["stage"])
        if current_stage is RolloutStage.FULL:
            log("deploy", f"{version} already fully rolled out; no-op")
            return DeployResult(
                version=version,
                stage=RolloutStage.FULL,
                rolled_back=False,
                previous_version=state.get("previous_version"),
                visible_version=env.visible_version,
                reason="already-fully-rolled-out",
            )

        next_stage = NEXT_STAGE[current_stage]
        state["deploy_attempts"] = int(state.get("deploy_attempts", 0)) + 1
        pre_visibility = env.visible_version
        log(
            "deploy",
            "rollout attempt",
            version=version,
            attempt=state["deploy_attempts"],
            **{"from": current_stage.value, "to": next_stage.value},
            pre_visible=pre_visibility,
        )

        env.install(artifact, version)
        previous = env.activate(version)
        verdict = env.check_health(next_stage)
        log(
            "deploy",
            "health verdict",
            stage=next_stage.value,
            healthy=verdict.healthy,
            conclusive=verdict.conclusive,
            detail=verdict.detail,
        )

        if verdict.healthy and verdict.conclusive:
            state["stage"] = next_stage.value
            state["previous_version"] = previous
            state["visible_version"] = version
            state["rolled_back"] = False
            state["history"].append(
                {"event": "advance", "stage": next_stage.value, "version": version}
            )
            return DeployResult(
                version=version,
                stage=next_stage,
                rolled_back=False,
                previous_version=previous,
                visible_version=version,
                reason=f"advanced-to-{next_stage.value}",
            )

        # Unhealthy OR inconclusive -> mandatory rollback to exact prior state.
        reason_code = (
            "health-inconclusive" if not verdict.conclusive else "health-failed"
        )
        env.activate_previous(previous)
        post_visibility = env.visible_version
        if post_visibility != previous:
            raise RollbackError(
                f"post-rollback visibility {post_visibility!r} != pre-deploy "
                f"{previous!r}",
                code="rollback_visibility_mismatch",
            )
        state["rolled_back"] = True
        state["visible_version"] = previous
        state["history"].append(
            {
                "event": "rollback",
                "stage": next_stage.value,
                "version": version,
                "reason": reason_code,
                "detail": verdict.detail,
            }
        )
        log(
            "deploy",
            "rolled back",
            version=version,
            stage=next_stage.value,
            visible_restored=previous,
        )
        return DeployResult(
            version=version,
            stage=current_stage,
            rolled_back=True,
            previous_version=previous,
            visible_version=previous,
            reason=f"{reason_code}: {verdict.detail}",
        )
