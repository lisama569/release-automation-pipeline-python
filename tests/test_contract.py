"""Request-contract enforcement: explicit, machine-distinguishable rejection."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from release_pipeline.errors import ContractViolationError
from release_pipeline.pipeline import PipelineConfig, run_pipeline
from release_pipeline.preview import preview_pipeline

pytestmark = pytest.mark.logging_inputs


def _cfg(work, **over):
    base = dict(repo_dir=".", work_dir=str(work), mode="local")
    base.update(over)
    return PipelineConfig(**base)


@pytest.mark.parametrize(
    "overrides, code",
    [
        (dict(targets=()), "empty_targets"),
        (dict(targets=("linux-x86_64", "linux-x86_64")), "duplicate_target"),
        (dict(targets=("linux-x86_64", "sparcv9")), "unknown_target"),
        (
            dict(failing_targets=frozenset({"linux-aarch64"}),
                 targets=("linux-x86_64",)),
            "unknown_fail_target",
        ),
        (dict(stages=()), "empty_stages"),
        (dict(stages=("staging", "prod")), "unknown_stage"),
        (dict(stages=("canary", "staging")), "invalid_stage_order"),
        (dict(stages=("staging", "full")), "invalid_stage_order"),
        (dict(stages=("staging", "staging")), "invalid_stage_order"),
        (dict(stages=("full",)), "invalid_stage_order"),
    ],
)
def test_run_rejects_contract_violations(tmp_path, git_repo, overrides, code):
    work = tmp_path / "w"
    with pytest.raises(ContractViolationError) as exc:
        run_pipeline(_cfg(work, repo_dir=str(git_repo), **overrides))
    print(f"\n[test input] {overrides} -> code={exc.value.code}")
    assert exc.value.code == code
    # Rejection happens before any filesystem effect: nothing was created,
    # so nothing could have been "partially executed".
    assert not work.exists()


@pytest.mark.parametrize(
    "overrides, code",
    [
        (dict(targets=()), "empty_targets"),
        (dict(stages=("full", "canary")), "invalid_stage_order"),
        (dict(targets=("nope",)), "unknown_target"),
    ],
)
def test_preview_rejects_the_same_violations(tmp_path, git_repo,
                                             overrides, code):
    work = tmp_path / "w"
    with pytest.raises(ContractViolationError) as exc:
        preview_pipeline(_cfg(work, repo_dir=str(git_repo), **overrides))
    print(f"\n[test input] preview {overrides} -> code={exc.value.code}")
    assert exc.value.code == code
    assert not work.exists()


def test_violation_codes_are_machine_distinguishable():
    codes = set()
    for overrides in (
        dict(targets=()),
        dict(stages=()),
        dict(targets=("bogus",)),
        dict(stages=("bogus",)),
        dict(stages=("canary",)),
    ):
        cfg = _cfg("/nonexistent-work", **overrides)
        with pytest.raises(ContractViolationError) as exc:
            run_pipeline(cfg)
        codes.add(exc.value.code)
    print(f"\n[test input] five violations -> distinct codes={sorted(codes)}")
    assert codes == {
        "empty_targets",
        "empty_stages",
        "unknown_target",
        "unknown_stage",
        "invalid_stage_order",
    }


def test_cli_rejects_invalid_request_with_code(tmp_path, git_repo):
    proc = subprocess.run(
        [sys.executable, "-m", "release_pipeline", "run",
         "--repo", str(git_repo), "--work-dir", str(tmp_path / "w"),
         "--targets", ""],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        check=False,
    )
    print(f"\n[test input] cli empty targets -> exit={proc.returncode} "
          f"err={proc.stderr.strip()}")
    assert proc.returncode == 1
    assert "empty_targets" in proc.stderr
    assert not (tmp_path / "w").exists()
    # stdout stays machine-clean (no partial JSON) on contract errors.
    assert proc.stdout.strip() == ""


def test_cli_preview_rejects_invalid_request(tmp_path, git_repo):
    proc = subprocess.run(
        [sys.executable, "-m", "release_pipeline", "preview",
         "--repo", str(git_repo), "--work-dir", str(tmp_path / "w"),
         "--stages", "canary,staging"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        check=False,
    )
    print(f"\n[test input] cli preview bad stages -> exit={proc.returncode}")
    assert proc.returncode == 1
    assert "invalid_stage_order" in proc.stderr
    assert not (tmp_path / "w").exists()


def test_all_failed_vs_partial_failure_are_distinguished(tmp_path, git_repo):
    """Full failure and partial failure must never collapse into one verdict."""
    partial = run_pipeline(_cfg(
        tmp_path / "p", repo_dir=str(git_repo),
        targets=("linux-x86_64", "linux-aarch64"),
        failing_targets=frozenset({"linux-aarch64"}),
    ))
    total = run_pipeline(_cfg(
        tmp_path / "t", repo_dir=str(git_repo),
        targets=("linux-x86_64", "linux-aarch64"),
        failing_targets=frozenset({"linux-x86_64", "linux-aarch64"}),
    ))
    print(f"\n[test input] partial outcome={partial.outcome} "
          f"total outcome={total.outcome}")
    assert partial.outcome == "partial-failure"
    assert total.outcome == "all-failed"
    assert partial.outcome != total.outcome
    # The surviving target of the partial run is still fully deployed;
    # the total run deployed nothing.
    assert (tmp_path / "p" / "environments" / "linux-x86_64"
            / "current").exists()
    assert not (tmp_path / "t" / "environments" / "linux-x86_64").exists()
    # Records carry the distinction too.
    pr = json.loads(Path(partial.record_path).read_text())
    tr = json.loads(Path(total.record_path).read_text())
    assert pr["outcome"] == "partial-failure"
    assert tr["outcome"] == "all-failed"
