"""Request-contract boundaries and total-vs-partial failure distinction."""

from __future__ import annotations

import pytest

from release_pipeline.errors import InvalidRequestError
from release_pipeline.pipeline import (
    EXIT_BUILD_FAILED,
    EXIT_BUILD_FAILED_ALL,
    PipelineConfig,
    run_pipeline,
)
from release_pipeline.plan import plan_pipeline

pytestmark = pytest.mark.logging_inputs


def _cfg(work, repo, **over):
    return PipelineConfig(repo_dir=str(repo), work_dir=str(work),
                          mode="local", **over)


def test_empty_targets_rejected_with_distinct_code(tmp_path, git_repo):
    with pytest.raises(InvalidRequestError) as exc:
        run_pipeline(_cfg(tmp_path / "w", git_repo, targets=()))
    assert exc.value.code == "empty_targets"
    # Rejected before any side effect.
    assert not (tmp_path / "w").exists()


def test_empty_stages_rejected(tmp_path, git_repo):
    with pytest.raises(InvalidRequestError) as exc:
        run_pipeline(_cfg(tmp_path / "w", git_repo, stages=()))
    assert exc.value.code == "empty_stages"


def test_unknown_stage_in_sequence_rejected(tmp_path, git_repo):
    with pytest.raises(InvalidRequestError) as exc:
        run_pipeline(_cfg(tmp_path / "w", git_repo, stages=("staging", "prod")))
    assert exc.value.code == "unknown_stage"


def test_non_monotonic_stage_sequence_rejected(tmp_path, git_repo):
    for bad in (("canary", "staging"), ("staging", "staging"),
                ("full", "canary")):
        with pytest.raises(InvalidRequestError) as exc:
            run_pipeline(_cfg(tmp_path / "w", git_repo, stages=bad))
        assert exc.value.code == "invalid_stage_sequence", bad
        print(f"\n[test input] stages={bad} -> {exc.value.code}")


def test_legal_stage_subsequences_accepted(tmp_path, git_repo):
    for good in (("staging",), ("canary",), ("full",),
                 ("staging", "canary"), ("staging", "full"),
                 ("canary", "full"), ("staging", "canary", "full")):
        r = run_pipeline(_cfg(tmp_path / good[0], git_repo,
                              targets=("t1",), stages=good))
        print(f"\n[test input] stages={good} -> exit={r.exit_code}")
        assert r.exit_code == 0


def test_unknown_failing_target_rejected(tmp_path, git_repo):
    with pytest.raises(InvalidRequestError) as exc:
        run_pipeline(_cfg(
            tmp_path / "w", git_repo,
            targets=("t1",), failing_targets=frozenset({"t9"}),
        ))
    assert exc.value.code == "unknown_target"


def test_unknown_stage_in_health_policy_rejected(tmp_path, git_repo):
    with pytest.raises(InvalidRequestError) as exc:
        run_pipeline(_cfg(
            tmp_path / "w", git_repo,
            health_policy={"prod": (False, True)},
        ))
    assert exc.value.code == "unknown_stage"


def test_duplicate_targets_rejected(tmp_path, git_repo):
    with pytest.raises(InvalidRequestError) as exc:
        run_pipeline(_cfg(tmp_path / "w", git_repo, targets=("t1", "t1")))
    assert exc.value.code == "duplicate_target"


def test_partial_failure_is_distinct_from_total_failure(tmp_path, git_repo):
    partial = run_pipeline(_cfg(
        tmp_path / "p", git_repo,
        targets=("t1", "t2"), failing_targets=frozenset({"t2"}),
    ))
    total = run_pipeline(_cfg(
        tmp_path / "a", git_repo,
        targets=("t1", "t2"), failing_targets=frozenset({"t1", "t2"}),
    ))
    print(f"\n[test input] partial exit={partial.exit_code} "
          f"kind={partial.failure_kind}; total exit={total.exit_code} "
          f"kind={total.failure_kind}")
    assert partial.exit_code == EXIT_BUILD_FAILED
    assert partial.failure_kind == "partial"
    assert total.exit_code == EXIT_BUILD_FAILED_ALL
    assert total.failure_kind == "all"


def test_plan_enforces_the_same_contracts(tmp_path, git_repo):
    bad_requests = [
        dict(targets=()),
        dict(stages=()),
        dict(stages=("full", "staging")),
        dict(stages=("mars",)),
        dict(targets=("t1",), failing_targets=frozenset({"t2"})),
    ]
    for bad in bad_requests:
        with pytest.raises(InvalidRequestError):
            plan_pipeline(_cfg(tmp_path / "w", git_repo, **bad))
    assert not (tmp_path / "w").exists()
