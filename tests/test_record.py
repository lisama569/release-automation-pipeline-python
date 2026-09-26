"""Release records: byte-stable, comparable, self-consistent with artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_pipeline.pipeline import PipelineConfig, run_pipeline
from release_pipeline.record import (
    RecordError,
    compare_records,
    load_record,
    record_to_bytes,
    verify_record,
)

pytestmark = pytest.mark.logging_inputs


def _cfg(work, repo, **over):
    base = dict(repo_dir=str(repo), work_dir=str(work), mode="local")
    base.update(over)
    return PipelineConfig(**base)


def _record_of(result) -> dict:
    assert result.record_path, "every real execution must produce a record"
    return load_record(result.record_path)


# --------------------------------------------------------------- determinism
def test_record_byte_identical_across_work_dirs(tmp_path, git_repo):
    r1 = run_pipeline(_cfg(tmp_path / "w1", git_repo))
    r2 = run_pipeline(_cfg(tmp_path / "w2", git_repo))
    b1 = Path(r1.record_path).read_bytes()
    b2 = Path(r2.record_path).read_bytes()
    print(f"\n[test input] same request, two work dirs -> id1={r1.record_id[:12]} "
          f"id2={r2.record_id[:12]}")
    assert r1.record_id == r2.record_id
    assert b1 == b2


def test_record_byte_identical_local_vs_ci(tmp_path, git_repo):
    kw = dict(env={"PATH": "/usr/bin", "CC": "cc"}, config={"opt": True})
    rl = run_pipeline(_cfg(tmp_path / "l", git_repo, mode="local", **kw))
    rc = run_pipeline(_cfg(tmp_path / "c", git_repo, mode="ci", **kw))
    print(f"\n[test input] local vs ci -> equal="
          f"{rl.record_id == rc.record_id}")
    assert rl.record_id == rc.record_id
    assert Path(rl.record_path).read_bytes() == Path(rc.record_path).read_bytes()


def test_record_stable_across_repeated_runs(tmp_path, git_repo):
    work = tmp_path / "w"
    r1 = run_pipeline(_cfg(work, git_repo))
    first_bytes = Path(r1.record_path).read_bytes()
    r2 = run_pipeline(_cfg(work, git_repo))
    r3 = run_pipeline(_cfg(work, git_repo, mode="ci"))
    print(f"\n[test input] three runs same dir -> ids="
          f"{[r.record_id[:8] for r in (r1, r2, r3)]}")
    assert r1.record_id == r2.record_id == r3.record_id
    assert Path(r1.record_path).read_bytes() == first_bytes
    # cache-hit vs built must not leak into the record.
    rec = _record_of(r2)
    assert {t["build_reason"] for t in rec["targets"]} == {"success"}


def test_record_contents_cover_version_artifacts_stages(tmp_path, git_repo):
    r = run_pipeline(_cfg(tmp_path / "w", git_repo))
    rec = _record_of(r)
    print(f"\n[test input] record keys -> {sorted(rec)}")
    assert rec["version"]["version"] == r.version.version
    assert rec["version"]["changelog"], "changelog must be recorded"
    assert rec["version"]["reproducible_hash"] == r.version.reproducible_hash
    for t in rec["targets"]:
        assert t["content_hash"] and t["cache_key"] and t["artifact_name"]
        assert t["final_stage"] == "full"
        assert t["rollout_blocked"] is False
    assert rec["outcome"] == "all-succeeded"
    assert rec["exit_code"] == 0


# ------------------------------------------------------------- comparability
def test_compare_identical_records_is_equal(tmp_path, git_repo):
    r1 = run_pipeline(_cfg(tmp_path / "w1", git_repo))
    r2 = run_pipeline(_cfg(tmp_path / "w2", git_repo))
    diff = compare_records(_record_of(r1), _record_of(r2))
    print(f"\n[test input] identical records -> equal={diff.equal}")
    assert diff.equal and diff.changes == ()


def test_compare_pinpoints_changed_inputs(tmp_path, git_repo):
    r1 = run_pipeline(_cfg(tmp_path / "w1", git_repo, config={"opt": True}))
    r2 = run_pipeline(_cfg(tmp_path / "w2", git_repo, config={"opt": False}))
    diff = compare_records(_record_of(r1), _record_of(r2))
    print(f"\n[test input] config changed -> changes={list(diff.changes)[:4]}")
    assert not diff.equal
    assert any(c.startswith("inputs.config_fingerprint:") for c in diff.changes)
    # Changed config -> changed cache keys and content identity per target.
    assert any("cache_key" in c for c in diff.changes)


def test_compare_pinpoints_changed_results(tmp_path, git_repo):
    r1 = run_pipeline(_cfg(tmp_path / "w1", git_repo))
    r2 = run_pipeline(_cfg(tmp_path / "w2", git_repo,
                           health_policy={"canary": (False, True)}))
    diff = compare_records(_record_of(r1), _record_of(r2))
    print(f"\n[test input] health policy changed -> changes="
          f"{list(diff.changes)[:6]}")
    assert not diff.equal
    assert any(c.startswith("outcome:") for c in diff.changes)
    assert any(c.startswith("exit_code:") for c in diff.changes)
    assert any("final_stage" in c for c in diff.changes)
    assert any("rollout_blocked" in c for c in diff.changes)
    assert any(c.startswith("inputs.health_policy") for c in diff.changes)


def test_compare_reports_target_set_changes(tmp_path, git_repo):
    r1 = run_pipeline(_cfg(tmp_path / "w1", git_repo,
                           targets=("linux-x86_64",)))
    r2 = run_pipeline(_cfg(tmp_path / "w2", git_repo,
                           targets=("linux-x86_64", "linux-aarch64")))
    diff = compare_records(_record_of(r1), _record_of(r2))
    print(f"\n[test input] target added -> changes={list(diff.changes)}")
    assert not diff.equal
    assert any("linux-aarch64" in c and "added" in c for c in diff.changes)


# ------------------------------------------------------- self-consistency
def test_verify_record_clean_after_run(tmp_path, git_repo):
    work = tmp_path / "w"
    r = run_pipeline(_cfg(work, git_repo))
    mismatches = verify_record(_record_of(r), work)
    print(f"\n[test input] fresh record -> mismatches={list(mismatches)}")
    assert mismatches == ()


def test_verify_detects_tampered_artifact(tmp_path, git_repo):
    work = tmp_path / "w"
    r = run_pipeline(_cfg(work, git_repo))
    rec = _record_of(r)
    victim = next((work / "artifacts").glob("*.bin"))
    victim.write_bytes(b"tampered")
    mismatches = verify_record(rec, work)
    print(f"\n[test input] tampered artifact -> {list(mismatches)}")
    assert any(m.startswith("artifact_hash_mismatch:") for m in mismatches)


def test_verify_detects_missing_cache_entry(tmp_path, git_repo):
    work = tmp_path / "w"
    r = run_pipeline(_cfg(work, git_repo))
    rec = _record_of(r)
    victim = next((work / "cache").rglob("artifact.bin"))
    victim.unlink()
    mismatches = verify_record(rec, work)
    print(f"\n[test input] deleted cache artifact -> {list(mismatches)}")
    assert any(m.startswith("cache_missing:") for m in mismatches)


def test_verify_detects_edited_record(tmp_path, git_repo):
    work = tmp_path / "w"
    r = run_pipeline(_cfg(work, git_repo))
    rec = _record_of(r)
    rec["targets"][0]["content_hash"] = "0" * 64
    mismatches = verify_record(rec, work)
    print(f"\n[test input] edited record body -> {list(mismatches)}")
    assert "record_id_mismatch" in mismatches
    assert any(m.startswith("artifact_hash_mismatch:") for m in mismatches)


def test_load_record_rejects_id_mismatch(tmp_path, git_repo):
    r = run_pipeline(_cfg(tmp_path / "w", git_repo))
    path = Path(r.record_path)
    rec = json.loads(path.read_text())
    rec["outcome"] = "all-failed"  # tamper without recomputing id
    path.write_text(json.dumps(rec))
    with pytest.raises(RecordError) as exc:
        load_record(path)
    print(f"\n[test input] tampered record file -> code={exc.value.code}")
    assert exc.value.code == "record_id_mismatch"


def test_record_serialization_is_canonical(tmp_path, git_repo):
    r = run_pipeline(_cfg(tmp_path / "w", git_repo))
    rec = _record_of(r)
    # Re-serializing a loaded record reproduces the file byte for byte.
    assert record_to_bytes(rec) == Path(r.record_path).read_bytes()


def test_partial_failure_record_stable_on_repeat(tmp_path, git_repo):
    work = tmp_path / "w"
    over = dict(
        targets=("linux-x86_64", "linux-aarch64"),
        failing_targets=frozenset({"linux-aarch64"}),
    )
    r1 = run_pipeline(_cfg(work, git_repo, **over))
    r2 = run_pipeline(_cfg(work, git_repo, **over))
    print(f"\n[test input] partial failure repeated -> ids equal="
          f"{r1.record_id == r2.record_id}")
    assert r1.record_id == r2.record_id
    rec = _record_of(r2)
    statuses = {t["target"]: t for t in rec["targets"]}
    assert statuses["linux-aarch64"]["build_status"] == "failed"
    assert "build-error" in statuses["linux-aarch64"]["build_reason"]
    assert statuses["linux-aarch64"]["final_stage"] == ""
    assert statuses["linux-x86_64"]["final_stage"] == "full"
