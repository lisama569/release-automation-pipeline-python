"""Staged rollout: gating, inconclusive health, rollback visibility."""

from __future__ import annotations

import pytest

from release_pipeline.deploy import SimulatedEnvironment, rollout
from release_pipeline.errors import RollbackError, StateError
from release_pipeline.models import Artifact, RolloutStage
from release_pipeline.state import ReleaseStateStore

pytestmark = pytest.mark.logging_inputs


@pytest.fixture
def artifact(tmp_path):
    p = tmp_path / "incoming.bin"
    p.write_bytes(b"release-bytes")
    return Artifact("t", "1.0.0", str(p), "h1", False, "k1")


def _make_artifact(tmp_path, version, data):
    p = tmp_path / f"incoming-{version}.bin"
    p.write_bytes(data)
    return Artifact("t", version, str(p), f"hash-{version}", False, f"key-{version}")


def test_full_healthy_progression_is_idempotent(tmp_path, artifact):
    env = SimulatedEnvironment(tmp_path / "env")
    store = ReleaseStateStore(tmp_path / "state")
    results = [rollout(env, artifact, "1.0.0", store) for _ in range(4)]
    print(f"\n[test input] healthy policy -> stages="
          f"{[r.stage.value for r in results]}")
    assert [r.stage for r in results] == [
        RolloutStage.STAGING, RolloutStage.CANARY, RolloutStage.FULL,
        RolloutStage.FULL,
    ]
    assert results[-1].reason == "already-fully-rolled-out"
    assert env.visible_version == "1.0.0"


def test_unhealthy_fresh_deploy_removes_visibility(tmp_path, artifact):
    env = SimulatedEnvironment(
        tmp_path / "env", health_by_stage={"staging": (False, True)}
    )
    store = ReleaseStateStore(tmp_path / "state")
    r = rollout(env, artifact, "1.0.0", store)
    print(f"\n[test input] unhealthy staging -> rolled_back={r.rolled_back} "
          f"visible={r.visible_version}")
    assert r.rolled_back and r.visible_version is None
    assert not (tmp_path / "env" / "current").exists()
    assert store.read()["stage"] == RolloutStage.NONE.value


def test_inconclusive_health_blocks_advance(tmp_path, artifact):
    env = SimulatedEnvironment(
        tmp_path / "env", health_by_stage={"canary": (True, False)}
    )
    store = ReleaseStateStore(tmp_path / "state")
    assert rollout(env, artifact, "1.0.0", store).stage == RolloutStage.STAGING
    r = rollout(env, artifact, "1.0.0", store)
    print(f"\n[test input] inconclusive canary -> rolled_back={r.rolled_back} "
          f"stage={r.stage.value} reason={r.reason}")
    assert r.rolled_back and "health-inconclusive" in r.reason
    assert r.stage is RolloutStage.STAGING, "inconclusive health must not advance"
    assert env.visible_version == "1.0.0"


def test_rollback_restores_exact_previous_version(tmp_path):
    env = SimulatedEnvironment(tmp_path / "env")
    store = ReleaseStateStore(tmp_path / "state")
    old = _make_artifact(tmp_path, "0.9.0", b"old-bytes")
    for _ in range(3):
        rollout(env, old, "0.9.0", store)
    assert env.visible_version == "0.9.0"

    env_bad = SimulatedEnvironment(
        tmp_path / "env", health_by_stage={"staging": (False, True)}
    )
    new = _make_artifact(tmp_path, "1.0.0", b"new-bytes")
    r = rollout(env_bad, new, "1.0.0", store)
    print(f"\n[test input] failed 1.0.0 over live 0.9.0 -> "
          f"visible={r.visible_version}")
    assert r.rolled_back
    assert r.visible_version == "0.9.0"
    assert env_bad.visible_version == "0.9.0"
    assert (tmp_path / "env" / "releases" / "0.9.0" / "artifact.bin")\
        .read_bytes() == b"old-bytes"


def test_retry_after_rollback_succeeds(tmp_path, artifact):
    env = SimulatedEnvironment(
        tmp_path / "env", health_by_stage={"staging": (False, True)}
    )
    store = ReleaseStateStore(tmp_path / "state")
    first = rollout(env, artifact, "1.0.0", store)
    assert first.rolled_back
    # Operator fixes the environment; retry must not be permanently blocked.
    env._policy.clear()
    second = rollout(env, artifact, "1.0.0", store)
    print(f"\n[test input] retry after policy fix -> stage={second.stage.value}")
    assert second.stage is RolloutStage.STAGING and not second.rolled_back


def test_install_conflict_on_different_content(tmp_path, artifact):
    env = SimulatedEnvironment(tmp_path / "env")
    env.install(artifact, "1.0.0")
    other = _make_artifact(tmp_path, "1.0.0", b"different")
    with pytest.raises(StateError) as exc:
        env.install(other, "1.0.0")
    assert exc.value.code == "release_content_conflict"
