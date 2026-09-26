"""Release records: determinism, comparability, verifiability."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_pipeline.pipeline import PipelineConfig, run_pipeline
from release_pipeline.record import (
    canonical_record_bytes,
    diff_records,
    verify_record,
)

pytestmark = pytest.mark.logging_inputs


def _cfg(work, repo, **over):
    kw = dict(work_dir=str(work), mode="local")
    kw.update(over)
    return PipelineConfig(repo_dir=str(repo), **kw)


def _record_file(work: Path, version: str) -> Path:
    return work / "records" / f"record-{version}.json"


def test_record_written_and_byte_stable_across_repeats(tmp_path, git_repo):
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo, env={"PATH": "/usr/bin"})
    r1 = run_pipeline(cfg)
    path = _record_file(work, r1.version.version)
    assert path.is_file()
    first_bytes = path.read_bytes()
    assert first_bytes == canonical_record_bytes(r1.record)

    r2 = run_pipeline(cfg)
    second_bytes = path.read_bytes()
    print(f"\n[test input] repeat run -> {len(first_bytes)}B stable="
          f"{first_bytes == second_bytes}")
    assert first_bytes == second_bytes
    assert r1.record == r2.record


def test_record_identical_between_local_and_ci(tmp_path, git_repo):
    kw = dict(
        repo_dir=str(git_repo),
        targets=("t1", "t2"),
        env={"PATH": "/usr/bin", "CC": "cc"},
        config={"opt": True},
        health_policy={"staging": (False, True)},
    )
    rl = run_pipeline(PipelineConfig(work_dir=str(tmp_path / "l"),
                                     mode="local", **kw))
    rc = run_pipeline(PipelineConfig(work_dir=str(tmp_path / "c"),
                                     mode="ci", **kw))
    assert canonical_record_bytes(rl.record) == \
        canonical_record_bytes(rc.record)


def test_record_independent_of_cwd_and_ambient_env(
    tmp_path, git_repo, monkeypatch
):
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo, env={"PATH": "/usr/bin"})
    r1 = run_pipeline(cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_RUN_ID", "12345")
    monkeypatch.setenv("EXTRA_NOISE", "x")
    r2 = run_pipeline(cfg)
    assert canonical_record_bytes(r1.record) == \
        canonical_record_bytes(r2.record)


def test_record_contains_version_changelog_artifacts_stages(
    tmp_path, git_repo
):
    work = tmp_path / "w"
    r = run_pipeline(_cfg(work, git_repo, targets=("t1",)))
    rec = r.record
    assert rec["version"]["version"] == r.version.version
    assert rec["version"]["changelog"], "changelog must be recorded"
    assert rec["version"]["reproducible_hash"]
    entry = rec["targets"][0]
    assert entry["status"] == "success"
    assert entry["artifact"]["content_hash"]
    assert entry["artifact"]["cache_key"]
    stage = rec["stages_final"][0]
    assert stage == {
        "target": "t1",
        "final_stage": "full",
        "rolled_back": False,
        "visible_version": r.version.version,
    }
    assert rec["outcome"] == {
        "exit_code": 0, "failure_kind": "none", "deploy_blocked": False,
    }


def test_record_excludes_volatile_fields(tmp_path, git_repo):
    # cache_hit / reason strings differ between the first (built) and the
    # second (cache-hit) run; they must not appear anywhere in the record.
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo)
    run_pipeline(cfg)
    blob = json.dumps(run_pipeline(cfg).record)
    for volatile in ("cache_hit", "cache-hit", "built", "reason",
                     "deploy_attempts", str(tmp_path)):
        assert volatile not in blob, volatile


def test_diff_records_pinpoints_input_and_result_changes(
    tmp_path, git_repo
):
    base = dict(repo_dir=str(git_repo), env={"PATH": "/usr/bin"})
    r1 = run_pipeline(PipelineConfig(work_dir=str(tmp_path / "w1"),
                                     targets=("t1", "t2"), **base))
    r2 = run_pipeline(PipelineConfig(work_dir=str(tmp_path / "w2"),
                                     targets=("t1", "t3"), **base))

    same = diff_records(r1.record, r1.record)
    assert same == {"identical": True, "changes": []}

    diff = diff_records(r1.record, r2.record)
    assert not diff["identical"]
    sections = {c["section"] for c in diff["changes"]}
    print(f"\n[test input] targets t1,t2 vs t1,t3 -> sections={sections}")
    # The changed input (target list) and the changed results (per-target
    # artifacts and final stages) are both identified.
    assert "inputs" in sections
    assert "targets" in sections
    assert "stages_final" in sections
    assert any(c["path"] == "inputs.targets[1]" for c in diff["changes"])


def test_diff_records_detects_health_policy_change(tmp_path, git_repo):
    base = dict(repo_dir=str(git_repo), targets=("t1",),
                env={"PATH": "/usr/bin"})
    r1 = run_pipeline(PipelineConfig(work_dir=str(tmp_path / "w1"), **base))
    r2 = run_pipeline(PipelineConfig(
        work_dir=str(tmp_path / "w2"),
        health_policy={"staging": (False, True)}, **base))
    diff = diff_records(r1.record, r2.record)
    paths = {c["path"] for c in diff["changes"]}
    assert any(p.startswith("inputs.health_policy") for p in paths)
    assert any(p.startswith("stages_final") for p in paths)
    assert "outcome.exit_code" in paths


def test_verify_record_ok_and_detects_tampering(tmp_path, git_repo):
    work = tmp_path / "w"
    r = run_pipeline(_cfg(work, git_repo, targets=("t1", "t2")))
    ok = verify_record(r.record, work)
    assert ok == {"ok": True, "mismatches": []}

    # Tamper with one artifact: the mismatch must be discovered.
    victim = work / "artifacts" / "app-0-t1.bin"
    victim.write_bytes(b"tampered")
    bad = verify_record(r.record, work)
    assert not bad["ok"]
    assert bad["mismatches"][0]["target"] == "t1"
    assert bad["mismatches"][0]["problem"] == "digest_mismatch"

    # Delete the other artifact: reported as missing, not silently passed.
    (work / "artifacts" / "app-1-t2.bin").unlink()
    missing = verify_record(r.record, work)
    problems = {(m["target"], m["problem"]) for m in missing["mismatches"]}
    assert ("t1", "digest_mismatch") in problems
    assert ("t2", "missing") in problems


def test_record_for_failed_run_is_stable_and_distinguishes_failure_kind(
    tmp_path, git_repo
):
    kw = dict(repo_dir=str(git_repo), targets=("t1", "t2"),
              env={"PATH": "/usr/bin"})
    partial = run_pipeline(PipelineConfig(
        work_dir=str(tmp_path / "p"),
        failing_targets=frozenset({"t2"}), **kw))
    total = run_pipeline(PipelineConfig(
        work_dir=str(tmp_path / "a"),
        failing_targets=frozenset({"t1", "t2"}), **kw))
    assert partial.record["outcome"]["failure_kind"] == "partial"
    assert total.record["outcome"]["failure_kind"] == "all"
    # Repeat the total-failure run: record bytes stay identical.
    again = run_pipeline(PipelineConfig(
        work_dir=str(tmp_path / "a"),
        failing_targets=frozenset({"t1", "t2"}), **kw))
    assert canonical_record_bytes(total.record) == \
        canonical_record_bytes(again.record)
    # Failed targets carry no artifact reference.
    assert all(t["artifact"] is None for t in total.record["targets"])
