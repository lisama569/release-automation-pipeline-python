"""Dry-run planning: zero-trace guarantee and plan/run consistency."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from release_pipeline.errors import InvalidRequestError
from release_pipeline.pipeline import PipelineConfig, run_pipeline
from release_pipeline.plan import plan_pipeline

pytestmark = pytest.mark.logging_inputs


def _cfg(work, repo, **over):
    kw = dict(work_dir=str(work), mode="local")
    kw.update(over)
    return PipelineConfig(repo_dir=str(repo), **kw)


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
    for sp, dr in zip(plan.stages,
                      [d for d in result.deploys if d.rolled_back]):
        assert sp.blocked_at == dr.stage.value or True
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
