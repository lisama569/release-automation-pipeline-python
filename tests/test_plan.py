"""Dry-run planning: zero-trace guarantee and plan/run consistency."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from release_pipeline.errors import InvalidRequestError
from release_pipeline.pipeline import PipelineConfig, run_pipeline
from release_pipeline.plan import plan_pipeline
from release_pipeline.state import read_state_readonly

pytestmark = pytest.mark.logging_inputs


def _cfg(work, repo, **over):
    kw = dict(work_dir=str(work), mode="local")
    kw.update(over)
    return PipelineConfig(repo_dir=str(repo), **kw)


def _assert_plan_matches_run(plan, result, work: Path, *,
                             deploy_targets: tuple[str, ...]) -> None:
    """Item-by-item equivalence of a dry-run plan and the run right after it.

    Checks, for identical inputs on the same (previously untouched) work dir:
    * per-target reuse/rebuild/fail <-> real cache-hit/built/failed;
    * per-target final stage, blocked stage and block-reason kind;
    * the persisted post-run stage equals the predicted final stage;
    * the predicted exit code equals the real exit code.
    """
    by_name = {tr.target: tr for tr in result.build.results}
    for tp in plan.targets:
        tr = by_name[tp.target]
        if tp.action == "reuse":
            assert tr.reason == "cache-hit", tp.target
        elif tp.action == "rebuild":
            assert tr.reason == "built", tp.target
        else:
            assert tp.action == "fail"
            assert tr.status.value == "failed", tp.target

    real_errors = {e["target"]: e for e in result.deploy_errors}
    plan_stages = {s.target: s for s in plan.stages}
    rollbacks = {}
    for d in result.deploys:
        if d.rolled_back:
            kind = "health-inconclusive" if "inconclusive" in d.reason \
                else "health-failed"
            rollbacks[d.version] = (d, kind)  # last rollback wins per target
    # Map rollbacks back to targets via the artifact version.
    rollback_by_target: dict[str, tuple] = {}
    for tr in result.build.results:
        if tr.artifact is not None and tr.artifact.version in rollbacks:
            rollback_by_target[tr.target] = rollbacks[tr.artifact.version]

    for name in deploy_targets:
        state = read_state_readonly(work / "state" / name)
        persisted = state.get("stage", "none")
        sp = plan_stages[name]
        # Predicted final stage is what really gets persisted.
        assert persisted == sp.final_stage, (
            f"{name}: persisted {persisted} != plan final {sp.final_stage}"
        )
        if sp.blocked_at and sp.block_reason.startswith("release-in-progress"):
            err = real_errors.get(name)
            assert err is not None and err["stage"] == sp.blocked_at, name
            assert rollback_by_target.get(name) is None
        elif sp.blocked_at:
            assert sp.blocked_at is not None
            rolled, kind = rollback_by_target[name]
            assert rolled.stage.value == sp.final_stage, name
            assert kind in sp.block_reason, (name, sp.block_reason)
        else:
            assert name not in rollback_by_target, name
            assert name not in real_errors, name

    assert plan.would_exit_code == result.exit_code, (
        plan.summary, result.exit_code
    )



def _snapshot(root: Path) -> dict[str, tuple[str, str | None]]:
    """Map every path under root -> (type/hash, symlink target or None)."""
    snap: dict[str, tuple[str, str | None]] = {}
    if not root.exists():
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if p.is_symlink():
                snap[rel] = ("symlink", os.readlink(p))
            elif p.is_file():
                snap[rel] = ("file",
                             hashlib.sha256(p.read_bytes()).hexdigest())
            else:
                snap[rel] = ("dir", None)
    return snap


def test_plan_on_fresh_workdir_leaves_zero_trace(tmp_path, git_repo):
    work = tmp_path / "w"
    assert not work.exists()
    plan = plan_pipeline(_cfg(work, git_repo))
    assert not work.exists()
    assert [t.action for t in plan.targets] == ["rebuild", "rebuild"]
    assert plan.would_exit_code == 0
    print(f"\n[test input] fresh workdir plan -> {plan.summary}")


def test_plan_on_populated_workdir_leaves_everything_unchanged(
    tmp_path, git_repo
):
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo))
    before = _snapshot(work)
    plan = plan_pipeline(_cfg(work, git_repo))
    after = _snapshot(work)
    assert before == after
    assert all(t.action == "reuse" for t in plan.targets)


def test_plan_matches_real_run_when_cache_empty(tmp_path, git_repo):
    work = tmp_path / "w"
    kwargs = dict(health_policy={"canary": (False, True)})
    plan = plan_pipeline(_cfg(work, git_repo, **kwargs))
    result = run_pipeline(_cfg(work, git_repo, **kwargs))
    # Per target: predicted rebuild <-> actual built.
    for tp, tr in zip(plan.targets, result.build.results):
        if tp.action == "rebuild":
            assert tr.reason == "built"
        elif tp.action == "reuse":
            assert tr.reason == "cache-hit"
        else:
            assert tr.status.value == "failed"
    # Per stage: blocked at the same place for the same reason kind.
    rolled_back = [d for d in result.deploys if d.rolled_back]
    assert len(rolled_back) == len(plan.stages)
    for sp, dr in zip(plan.stages, rolled_back):
        assert sp.blocked_at == "canary"
        assert dr.stage.value == sp.final_stage == "staging"
        assert "health-failed" in sp.block_reason
        assert "health-failed" in dr.reason
    assert plan.would_exit_code == result.exit_code == 3
    assert all(s.blocked_at == "canary" for s in plan.stages)
    assert all("health-failed" in s.block_reason for s in plan.stages)


def test_plan_matches_second_real_run_cache_hits(tmp_path, git_repo):
    work = tmp_path / "w"
    kwargs = dict(health_policy={"canary": (False, True)})
    run_pipeline(_cfg(work, git_repo, **kwargs))
    plan = plan_pipeline(_cfg(work, git_repo, **kwargs))
    assert [t.action for t in plan.targets] == ["reuse", "reuse"]
    result = run_pipeline(_cfg(work, git_repo, **kwargs))
    assert [t.reason for t in result.build.results] == ["cache-hit",
                                                        "cache-hit"]
    assert all(s.blocked_at == "canary" and s.from_stage == "staging"
               for s in plan.stages)
    assert plan.would_exit_code == result.exit_code
    print(f"\n[test input] cache warm -> plan={plan.summary}")


def test_plan_matches_run_partial_failure(tmp_path, git_repo):
    work = tmp_path / "w"
    kwargs = dict(targets=("t1", "t2"),
                  failing_targets=frozenset({"t2"}))
    plan = plan_pipeline(_cfg(work, git_repo, **kwargs))
    result = run_pipeline(_cfg(work, git_repo, **kwargs))
    assert [t.action for t in plan.targets] == ["rebuild", "fail"]
    assert plan.would_exit_code == result.exit_code == 2
    # The failing target has no stage plan; the healthy one reaches full.
    assert len(plan.stages) == 1
    assert plan.stages[0].target == "t1"
    assert plan.stages[0].final_stage == "full"


def test_plan_matches_run_all_targets_fail(tmp_path, git_repo):
    work = tmp_path / "w"
    kwargs = dict(targets=("t1", "t2"),
                  failing_targets=frozenset({"t1", "t2"}))
    plan = plan_pipeline(_cfg(work, git_repo, **kwargs))
    result = run_pipeline(_cfg(work, git_repo, **kwargs))
    assert plan.would_exit_code == 4
    assert plan.would_exit_code == result.exit_code
    assert plan.stages == ()
    # No per-target environment materialized for failed targets.
    assert not (work / "environments" / "t1").exists()
    assert not (work / "environments" / "t2").exists()


def test_plan_matches_run_happy_path_stage_end_state(tmp_path, git_repo):
    work = tmp_path / "w"
    plan = plan_pipeline(_cfg(work, git_repo, targets=("t1",),
                              stages=("staging", "full")))
    result = run_pipeline(_cfg(work, git_repo, targets=("t1",),
                               stages=("staging", "full")))
    assert plan.stages[0].final_stage == "full"
    assert plan.stages[0].blocked_at is None
    assert plan.would_exit_code == result.exit_code == 0


def test_plan_inconclusive_health_matches_rollback(tmp_path, git_repo):
    work = tmp_path / "w"
    kwargs = dict(health_policy={"staging": (True, False)})
    plan = plan_pipeline(_cfg(work, git_repo, **kwargs))
    result = run_pipeline(_cfg(work, git_repo, **kwargs))
    assert all("health-inconclusive" in s.block_reason
               for s in plan.stages)
    assert all(s.blocked_at == "staging" for s in plan.stages)
    assert plan.would_exit_code == result.exit_code


def test_plan_error_leaves_no_trace(tmp_path, git_repo):
    work = tmp_path / "w"
    with pytest.raises(InvalidRequestError):
        plan_pipeline(_cfg(work, git_repo, targets=()))
    assert not work.exists()


def test_plan_does_not_evict_corrupted_cache(tmp_path, git_repo):
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo)
    run_pipeline(cfg)
    # Corrupt an artifact inside the cache; a real run would evict it.
    cache_files = list((work / "cache").rglob("artifact.bin"))
    assert cache_files
    cache_files[0].write_bytes(b"corrupt")
    before = _snapshot(work / "cache")
    plan = plan_pipeline(_cfg(work, git_repo))
    after = _snapshot(work / "cache")
    assert before == after, "plan must not evict or repair"
    states = {t.target: t.cache_state for t in plan.targets}
    assert "corrupted" in states.values()


def test_plan_concurrent_with_real_runs(tmp_path, git_repo):
    work = tmp_path / "w"
    errors: list[Exception] = []
    plan_summaries: list[str] = []

    def runner():
        try:
            run_pipeline(_cfg(work, git_repo, targets=("t1",)))
        except Exception as exc:
            errors.append(exc)

    def planner():
        try:
            p = plan_pipeline(_cfg(work, git_repo, targets=("t1",)))
            plan_summaries.append(p.summary)
        except Exception as exc:
            errors.append(exc)

    threads = ([threading.Thread(target=planner) for _ in range(2)] +
               [threading.Thread(target=runner) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # Real runs converge; plans never corrupted the shared state.
    state = (work / "state" / "t1" / "state.json").read_text()
    import json
    assert json.loads(state)["stage"] == "full"


def test_plan_is_independent_of_cwd_and_ambient_env(
    tmp_path, git_repo, monkeypatch
):
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo, env={"PATH": "/usr/bin", "CC": "cc"})
    p1 = plan_pipeline(cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXTRA_NOISE", "x")
    monkeypatch.setenv("CC", "totally-different")
    p2 = plan_pipeline(cfg)
    assert p1.to_dict() == p2.to_dict()


# ---------------------------------------------------------------------------
# Regression: stage sequences that skip intermediate stages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stages",
    [("staging", "full"), ("canary",), ("full",),
     ("staging",), ("canary", "full")],
)
def test_plan_and_run_reach_last_requested_stage_for_skipped_sequence(
    tmp_path, git_repo, stages
):
    work = tmp_path / ("w-" + "-".join(stages))
    cfg = _cfg(work, git_repo, targets=("t1",), stages=stages)
    plan = plan_pipeline(cfg)
    result = run_pipeline(cfg)
    # The plan names the last requested stage as the destination...
    sp = plan.stages[0]
    assert sp.final_stage == stages[-1]
    assert sp.blocked_at is None
    assert sp.block_reason == ""
    # ...and the real run really gets there, with exit 0 (no false success
    # before reaching the requested final stage).
    state = read_state_readonly(work / "state" / "t1")
    assert state["stage"] == stages[-1]
    assert state["rolled_back"] is False
    assert (work / "environments" / "t1" / "current").is_symlink()
    _assert_plan_matches_run(plan, result, work, deploy_targets=("t1",))
    print(f"\n[test input] stages={stages} -> both reach {stages[-1]} "
          f"exit={result.exit_code}")


@pytest.mark.parametrize(
    "stages,policy,blocked,final",
    [
        # Skip canary in the request, but the canary gate still runs and
        # blocks: must never report success at staging.
        (("staging", "full"), {"canary": (False, True)}, "canary", "staging"),
        # Request only full; every intermediate gate still applies.
        (("full",), {"staging": (False, True)}, "staging", "none"),
        (("full",), {"staging": (True, False)}, "staging", "none"),
        (("full",), {"canary": (False, True)}, "canary", "staging"),
        (("full",), {"full": (True, False)}, "full", "canary"),
        # Request only canary: staging gate first, then canary.
        (("canary",), {"canary": (False, True)}, "canary", "staging"),
        (("canary",), {"staging": (False, True)}, "staging", "none"),
    ],
)
def test_plan_and_run_block_at_same_gate_for_skipped_sequence(
    tmp_path, git_repo, stages, policy, blocked, final
):
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo, targets=("t1",), stages=stages,
               health_policy=policy)
    plan = plan_pipeline(cfg)
    result = run_pipeline(cfg)
    sp = plan.stages[0]
    assert sp.blocked_at == blocked
    assert sp.final_stage == final
    assert plan.would_exit_code == result.exit_code == 3
    state = read_state_readonly(work / "state" / "t1")
    assert state["stage"] == final
    assert state["rolled_back"] is True
    rolled = [d for d in result.deploys if d.rolled_back]
    assert rolled and rolled[-1].stage.value == final
    print(f"\n[test input] stages={stages} policy={policy} -> both blocked "
          f"at {blocked}, final {final}")


def test_plan_and_run_match_when_plan_runs_first_with_partial_failure(
    tmp_path, git_repo
):
    # Skipped stages AND a partial build failure: t2 fails, t1 reaches full.
    work = tmp_path / "w"
    kw = dict(targets=("t1", "t2"), failing_targets=frozenset({"t2"}),
              stages=("staging", "full"))
    plan = plan_pipeline(_cfg(work, git_repo, **kw))
    result = run_pipeline(_cfg(work, git_repo, **kw))
    assert [t.action for t in plan.targets] == ["rebuild", "fail"]
    assert {s.target for s in plan.stages} == {"t1"}
    assert plan.stages[0].final_stage == "full"
    _assert_plan_matches_run(plan, result, work, deploy_targets=("t1",))
    assert plan.would_exit_code == result.exit_code == 2


def test_plan_then_run_second_time_still_match(tmp_path, git_repo):
    # plan -> run -> plan -> run on a skipped sequence: every pair agrees.
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo, targets=("t1",), stages=("staging", "full"),
               health_policy={"canary": (False, True)})
    p1 = plan_pipeline(cfg)
    r1 = run_pipeline(cfg)
    assert p1.would_exit_code == r1.exit_code == 3
    p2 = plan_pipeline(cfg)
    r2 = run_pipeline(cfg)
    for p, r in ((p1, r1), (p2, r2)):
        assert p.stages[0].blocked_at == "canary"
        assert p.would_exit_code == r.exit_code == 3


def test_plan_with_skipped_stages_leaves_zero_trace(tmp_path, git_repo):
    # The zero-trace guarantee also holds for sequences that skip stages.
    work = tmp_path / "w"
    assert not work.exists()
    plan_pipeline(_cfg(work, git_repo, targets=("t1", "t2"),
                       stages=("staging", "full")))
    assert not work.exists()


def test_plan_after_partial_real_run_leaves_everything_unchanged(
    tmp_path, git_repo
):
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo, targets=("t1",), stages=("canary",)))
    before = _snapshot(work)
    plan_pipeline(_cfg(work, git_repo, targets=("t1",),
                       stages=("staging", "full")))
    assert _snapshot(work) == before


def test_plan_and_run_agree_on_release_in_progress(tmp_path, git_repo):
    # A different release stuck mid-flight: plan predicts the refusal (and
    # exit 1); the real run is refused at the same stage instead of crashing
    # or claiming success.
    from release_pipeline.models import RolloutStage
    from release_pipeline.state import ReleaseStateStore, new_state

    work = tmp_path / "w"
    store = ReleaseStateStore(work / "state" / "t1")
    with store.transaction() as st:
        st.update(new_state("9.9.9+9.gdeadbeef"))
        st["stage"] = RolloutStage.CANARY.value
    pre = _snapshot(work)
    cfg = _cfg(work, git_repo, targets=("t1",), stages=("full",))
    plan = plan_pipeline(cfg)
    # Planning around the foreign state still leaves it byte-for-byte intact.
    assert _snapshot(work) == pre
    sp = plan.stages[0]
    assert sp.blocked_at == "canary"
    assert sp.final_stage == "canary"
    assert sp.block_reason.startswith("release-in-progress")
    assert plan.would_exit_code == 1
    result = run_pipeline(cfg)
    assert result.exit_code == 1
    assert result.deploy_errors == (
        {"target": "t1", "stage": "canary",
         "version": result.version.version, "code": "release_in_progress"},
    )
    # The foreign state is untouched, and the refused run never made the
    # foreign release visible (no current symlink, no installed release).
    after = read_state_readonly(work / "state" / "t1")
    assert after["version"] == "9.9.9+9.gdeadbeef"
    assert after["stage"] == "canary"
    env_t1 = work / "environments" / "t1"
    assert not (env_t1 / "current").exists()
    assert not (env_t1 / "releases" / result.version.version).exists()

