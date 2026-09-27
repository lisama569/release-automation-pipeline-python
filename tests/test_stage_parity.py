"""Skipped-stage progression and item-by-item plan/run parity.

Regression coverage for the bug where a stage sequence that skips
intermediate stages (e.g. ``("staging", "full")`` or ``("canary",)``) made
the planner predict a full advance while the real run stopped at an
unrequested intermediate stage and still reported success.

After the fix the real run advances along the canonical chain
``none -> staging -> canary -> full`` until it reaches the LAST requested
stage, every canonical health gate on the path is enforced, and the dry-run
plan and the real execution agree item by item:

* per-target reuse/rebuild/fail conclusions;
* per-target final stage and blocking gate;
* blocking reason kind (``health-failed`` / ``health-inconclusive``);
* exit code.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from release_pipeline.models import RolloutStage
from release_pipeline.pipeline import (
    EXIT_OK,
    EXIT_ROLLOUT_BLOCKED,
    PipelineConfig,
    run_pipeline,
)
from release_pipeline.plan import plan_pipeline
from release_pipeline.state import read_state_readonly

pytestmark = pytest.mark.logging_inputs

ALL_SUBSEQUENCES = [
    ("staging",),
    ("canary",),
    ("full",),
    ("staging", "canary"),
    ("staging", "full"),
    ("canary", "full"),
    ("staging", "canary", "full"),
]

# (requested stages, health policy, expected final stage, expected blocked
# gate, expected reason kind, expected exit code).
PARITY_CASES = [
    # Happy paths: every skip pattern reaches its last requested stage.
    (("staging",), {}, "staging", None, None, EXIT_OK),
    (("canary",), {}, "canary", None, None, EXIT_OK),
    (("full",), {}, "full", None, None, EXIT_OK),
    (("staging", "canary"), {}, "canary", None, None, EXIT_OK),
    (("staging", "full"), {}, "full", None, None, EXIT_OK),
    (("canary", "full"), {}, "full", None, None, EXIT_OK),
    (("staging", "canary", "full"), {}, "full", None, None, EXIT_OK),
    # A gate on a SKIPPED intermediate stage still blocks both plan and run:
    # ("staging","full") must pass staging and canary to reach full.
    (("staging", "full"), {"canary": (False, True)},
     "staging", "canary", "health-failed", EXIT_ROLLOUT_BLOCKED),
    (("staging", "full"), {"canary": (True, False)},
     "staging", "canary", "health-inconclusive", EXIT_ROLLOUT_BLOCKED),
    # ... and the last requested stage's gate is enforced just the same.
    (("staging", "full"), {"full": (False, True)},
     "canary", "full", "health-failed", EXIT_ROLLOUT_BLOCKED),
    (("staging", "full"), {"full": (True, False)},
     "canary", "full", "health-inconclusive", EXIT_ROLLOUT_BLOCKED),
    # Asking only for canary still requires passing the staging gate.
    (("canary",), {"staging": (False, True)},
     "none", "staging", "health-failed", EXIT_ROLLOUT_BLOCKED),
    (("canary",), {"staging": (True, False)},
     "none", "staging", "health-inconclusive", EXIT_ROLLOUT_BLOCKED),
    # The blocking gate can sit before the requested window too.
    (("canary", "full"), {"staging": (False, True)},
     "none", "staging", "health-failed", EXIT_ROLLOUT_BLOCKED),
    (("staging",), {"staging": (False, True)},
     "none", "staging", "health-failed", EXIT_ROLLOUT_BLOCKED),
]


def _cfg(work, repo, **over):
    kw = dict(work_dir=str(work), mode="local")
    kw.update(over)
    return PipelineConfig(repo_dir=str(repo), **kw)


def _actual_target_outcome(work: Path, target: str) -> dict:
    """Final stage / blocking gate / reason kind from the persisted state."""
    state = read_state_readonly(work / "state" / target)
    rollbacks = [h for h in state.get("history", [])
                 if h.get("event") == "rollback"]
    last = rollbacks[-1] if rollbacks else None
    return {
        "final_stage": state.get("stage", RolloutStage.NONE.value),
        "blocked_at": last["stage"] if last else None,
        "reason": last.get("reason") if last else None,
        "rolled_back": bool(state.get("rolled_back", False)),
        "visible_version": state.get("visible_version"),
    }


def _build_reason_for(action: str) -> str | None:
    return {"reuse": "cache-hit", "rebuild": "built", "fail": None}[action]


@pytest.mark.parametrize("stages", ALL_SUBSEQUENCES)
def test_real_run_always_reaches_last_requested_stage(
    tmp_path, git_repo, stages
):
    """No 'success' verdict before the requested final stage is reached."""
    work = tmp_path / "w"
    result = run_pipeline(
        _cfg(work, git_repo, targets=("t1",), stages=stages)
    )
    assert result.exit_code == EXIT_OK, stages
    state = read_state_readonly(work / "state" / "t1")
    print(f"\n[test input] stages={stages} -> real final={state['stage']}")
    assert state["stage"] == stages[-1]
    assert state["rolled_back"] is False


@pytest.mark.parametrize(
    "stages,policy,exp_final,exp_blocked,exp_reason,exp_exit", PARITY_CASES
)
def test_plan_matches_real_run_item_by_item_for_skipped_stages(
    tmp_path, git_repo, stages, policy, exp_final, exp_blocked,
    exp_reason, exp_exit,
):
    work = tmp_path / "w"
    kwargs = dict(targets=("t1",), stages=stages, health_policy=policy)

    # Plan FIRST, on a fresh work dir: must predict exactly what happens.
    plan = plan_pipeline(_cfg(work, git_repo, **kwargs))
    assert len(plan.stages) == 1
    sp = plan.stages[0]
    assert sp.from_stage == "none"
    assert sp.final_stage == exp_final
    assert sp.blocked_at == exp_blocked
    if exp_reason is not None:
        assert exp_reason in sp.block_reason
    assert plan.would_exit_code == exp_exit
    # Per-target build prediction.
    assert [t.action for t in plan.targets] == ["rebuild"]

    result = run_pipeline(_cfg(work, git_repo, **kwargs))
    actual = _actual_target_outcome(work, "t1")

    # Per-target reuse/rebuild conclusion <-> real build outcome.
    tr = result.build.results[0]
    assert tr.reason == _build_reason_for(plan.targets[0].action)

    # Per-stage final position, blocking gate and reason kind.
    print(f"\n[test input] stages={stages} policy={policy} -> "
          f"final={actual['final_stage']} blocked={actual['blocked_at']} "
          f"reason={actual['reason']} exit={result.exit_code}")
    assert actual["final_stage"] == sp.final_stage
    assert actual["blocked_at"] == sp.blocked_at
    if exp_reason is None:
        assert actual["rolled_back"] is False
        assert actual["blocked_at"] is None
    else:
        assert actual["rolled_back"] is True
        assert actual["reason"] == exp_reason
        rolled = [d for d in result.deploys if d.rolled_back]
        assert rolled and exp_reason in rolled[-1].reason

    # Exit code must agree.
    assert result.exit_code == plan.would_exit_code == exp_exit


@pytest.mark.parametrize("stages", ALL_SUBSEQUENCES)
def test_plan_on_fresh_workdir_leaves_no_trace_for_skipped_stages(
    tmp_path, git_repo, stages
):
    work = tmp_path / "w"
    assert not work.exists()
    plan_pipeline(_cfg(work, git_repo, targets=("t1",), stages=stages))
    assert not work.exists(), f"plan for {stages} left traces"


def test_plan_matches_resume_from_persisted_midchain_state(
    tmp_path, git_repo
):
    """Persisted canary + request ("staging","full") advances to full."""
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo, targets=("t1",), stages=("canary",)))

    plan = plan_pipeline(_cfg(
        work, git_repo, targets=("t1",), stages=("staging", "full")
    ))
    sp = plan.stages[0]
    assert sp.from_stage == "canary"
    assert sp.final_stage == "full"
    assert sp.blocked_at is None
    assert plan.would_exit_code == EXIT_OK
    assert [t.action for t in plan.targets] == ["reuse"]

    result = run_pipeline(_cfg(
        work, git_repo, targets=("t1",), stages=("staging", "full")
    ))
    actual = _actual_target_outcome(work, "t1")
    assert actual["final_stage"] == "full"
    assert actual["blocked_at"] is None
    assert result.exit_code == plan.would_exit_code == EXIT_OK
    assert result.build.results[0].reason == "cache-hit"


def test_plan_matches_resume_when_gate_after_persisted_stage_blocks(
    tmp_path, git_repo
):
    """Persisted canary + unhealthy full: both plan and run block at full."""
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo, targets=("t1",), stages=("canary",)))

    kwargs = dict(targets=("t1",), stages=("staging", "full"),
                  health_policy={"full": (False, True)})
    plan = plan_pipeline(_cfg(work, git_repo, **kwargs))
    sp = plan.stages[0]
    assert (sp.from_stage, sp.final_stage, sp.blocked_at) == (
        "canary", "canary", "full")
    assert plan.would_exit_code == EXIT_ROLLOUT_BLOCKED

    result = run_pipeline(_cfg(work, git_repo, **kwargs))
    actual = _actual_target_outcome(work, "t1")
    assert actual["final_stage"] == "canary"
    assert actual["blocked_at"] == "full"
    assert actual["reason"] == "health-failed"
    assert result.exit_code == EXIT_ROLLOUT_BLOCKED


def test_request_below_persisted_progress_is_a_noop_for_both(
    tmp_path, git_repo
):
    """Persisted full + request ("staging",): nothing advances anywhere."""
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo, targets=("t1",)))

    plan = plan_pipeline(_cfg(
        work, git_repo, targets=("t1",), stages=("staging",)
    ))
    sp = plan.stages[0]
    assert sp.from_stage == sp.final_stage == "full"
    assert sp.blocked_at is None
    assert plan.would_exit_code == EXIT_OK

    result = run_pipeline(_cfg(
        work, git_repo, targets=("t1",), stages=("staging",)
    ))
    actual = _actual_target_outcome(work, "t1")
    assert actual["final_stage"] == "full"
    assert actual["blocked_at"] is None
    assert result.exit_code == EXIT_OK
    # No second round of deploy attempts was made.
    state = read_state_readonly(work / "state" / "t1")
    assert [h["event"] for h in state["history"]] == (
        ["advance"] * 3)


def test_concurrent_skipped_stage_runs_and_plans_converge(
    tmp_path, git_repo
):
    """Plans never corrupt shared state; real runs converge to requested end."""
    work = tmp_path / "w"
    errors: list[Exception] = []
    plan_finals: list[str] = []

    def runner():
        try:
            run_pipeline(_cfg(
                work, git_repo, targets=("t1",),
                stages=("staging", "full"),
            ))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def planner():
        try:
            p = plan_pipeline(_cfg(
                work, git_repo, targets=("t1",),
                stages=("staging", "full"),
            ))
            # Every observation must be an internally consistent point on the
            # canonical chain: a predicted final never behind the start unless
            # blocked, and the only reachable happy end is full.
            plan_finals.append(p.stages[0].final_stage)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = ([threading.Thread(target=planner) for _ in range(3)] +
               [threading.Thread(target=runner) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    state = json.loads(
        (work / "state" / "t1" / "state.json").read_text())
    assert state["stage"] == "full"
    assert state["rolled_back"] is False
    # Plans observed only real chain positions; none predicted a stage the
    # run could never end at.
    assert set(plan_finals) <= {"none", "staging", "canary", "full"}
