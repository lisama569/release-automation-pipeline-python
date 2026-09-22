"""Persistent state: atomicity, locking, crash/abort semantics."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from release_pipeline.errors import StateError
from release_pipeline.models import RolloutStage
from release_pipeline.state import ReleaseStateStore, new_state

pytestmark = pytest.mark.logging_inputs


def test_new_state_shape():
    st = new_state("1.2.3")
    assert st["version"] == "1.2.3"
    assert st["stage"] == RolloutStage.NONE.value
    assert st["history"] == []


def test_atomic_commit_and_abort(tmp_path):
    store = ReleaseStateStore(tmp_path)
    with store.transaction() as st:
        st.update(new_state("1.0.0"))
        st["history"].append("init")
    assert store.read()["version"] == "1.0.0"

    with pytest.raises(RuntimeError):
        with store.transaction() as st:
            st["version"] = "BROKEN"
            raise RuntimeError("simulated mid-update crash")
    on_disk = store.read()
    print(f"\n[test input] aborted update -> on-disk version={on_disk['version']}")
    assert on_disk["version"] == "1.0.0", "aborted tx must leave disk untouched"
    assert not (tmp_path / "state.json.tmp").exists(), "temp file must be cleaned"


def test_concurrent_transactions_no_lost_updates(tmp_path):
    store = ReleaseStateStore(tmp_path)
    with store.transaction() as st:
        st.update(new_state("1.0.0"))
        st["counter"] = 0

    def worker():
        for _ in range(25):
            with store.transaction() as st:
                st["counter"] += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = store.read()["counter"]
    print(f"\n[test input] 8x25 locked increments -> counter={final}")
    assert final == 200, f"lost updates: {final} != 200"


def test_corrupted_state_is_refused_not_reset(tmp_path):
    store = ReleaseStateStore(tmp_path)
    (tmp_path / "state.json").write_text("{not json")
    with pytest.raises(StateError) as exc:
        store.read()
    assert exc.value.code == "state_corrupted"
    with pytest.raises(StateError):
        with store.transaction() as st:
            st.update(new_state("x"))
    print("\n[test input] corrupt state refused instead of silently reset")
