"""Dry-run guarantees: zero trace, agreement with real execution, stability."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from release_pipeline.errors import StateError
from release_pipeline.pipeline import (
    EXIT_OK,
    EXIT_ROLLOUT_BLOCKED,
    PipelineConfig,
    run_pipeline,
)
from release_pipeline.preview import preview_pipeline

pytestmark = pytest.mark.logging_inputs


def _cfg(work, repo, **over):
    base = dict(repo_dir=str(repo), work_dir=str(work), mode="local")
    base.update(over)
    return PipelineConfig(**base)


def _snapshot(root: Path) -> dict:
    """Map every path under root to (kind, content-hash-or-target)."""
    snap: dict[str, tuple] = {}
    if not root.exists():
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if p.is_symlink():
                snap[rel] = ("symlink", os.readlink(p))
            elif p.is_dir():
                snap[rel] = ("dir",)
            else:
                snap[rel] = (
                    "file",
                    hashlib.sha256(p.read_bytes()).hexdigest(),
                )
    return snap


# ---------------------------------------------------------------- zero trace
def test_preview_leaves_no_trace_on_fresh_dir(tmp_path, git_repo):
    work = tmp_path / "w"
    result = preview_pipeline(_cfg(work, git_repo))
    print(f"\n[test input] fresh preview -> exit={result.predicted_exit_code} "
          f"actions={[b.action for b in result.builds]}")
    assert not work.exists(), "preview must not create the work dir"


def test_preview_leaves_no_trace_on_populated_dir(tmp_path, git_repo):
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo))
    before = _snapshot(work)
    repo_before = _snapshot(git_repo)
    preview_pipeline(_cfg(work, git_repo))
    preview_pipeline(_cfg(work, git_repo, health_policy={"canary": (False, True)}))
    after = _snapshot(work)
    print(f"\n[test input] populated preview -> files={len(before)} "
          f"unchanged={before == after}")
    assert before == after, "preview modified the work dir"
    assert _snapshot(git_repo) == repo_before, "preview touched the repo"


def test_preview_error_midway_leaves_no_trace(tmp_path, git_repo):
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo))
    # Corrupt one target's state file: preview must fail like the real run
    # would, and still leave everything exactly as it was.
    state_file = work / "state" / "linux-x86_64" / "state.json"
    state_file.write_text("{ not json")
    before = _snapshot(work)
    with pytest.raises(StateError) as exc:
        preview_pipeline(_cfg(work, git_repo))
    print(f"\n[test input] corrupted state -> code={exc.value.code}")
    assert exc.value.code == "state_corrupted"
    assert _snapshot(work) == before


def test_preview_does_not_evict_corrupt_cache(tmp_path, git_repo):
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo, targets=("linux-x86_64",)))
    # Corrupt the cached artifact bytes.
    victims = list((work / "cache").rglob("artifact.bin"))
    assert victims
    victims[0].write_bytes(b"tampered")
    before = _snapshot(work / "cache")
    result = preview_pipeline(_cfg(work, git_repo, targets=("linux-x86_64",)))
    print(f"\n[test input] corrupt cache -> action="
          f"{[(b.action, b.reason) for b in result.builds]}")
    assert result.builds[0].action == "rebuild"
    assert result.builds[0].reason == "cache-corrupt"
    # The real run would evict+rebuild; the preview must not evict.
    assert _snapshot(work / "cache") == before


# ------------------------------------------------------- preview == execution
def _assert_agrees(preview, result):
    assert preview.predicted_exit_code == result.exit_code
    assert preview.outcome == result.outcome
    assert preview.version == result.version.version
    by_target = {b.target: b for b in preview.builds}
    for tr in result.build.results:
        pv = by_target[tr.target]
        if tr.artifact is None:
            assert pv.action == "fail"
        elif tr.reason == "cache-hit":
            assert pv.action == "reuse"
        else:
            assert pv.action == "rebuild"
        if tr.artifact is not None:
            assert pv.content_hash == tr.artifact.content_hash
            assert pv.cache_key == tr.artifact.cache_key
    outcomes = {o.target: o for o in result.target_outcomes}
    for d in preview.deploys:
        assert d.final_stage == outcomes[d.target].final_stage
        assert bool(d.blocked_at) == outcomes[d.target].rollout_blocked


def test_preview_matches_first_execution(tmp_path, git_repo):
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo)
    preview = preview_pipeline(cfg)
    result = run_pipeline(cfg)
    print(f"\n[test input] first run -> preview={preview.predicted_exit_code} "
          f"actual={result.exit_code}")
    _assert_agrees(preview, result)


def test_preview_matches_repeat_execution(tmp_path, git_repo):
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo))
    preview = preview_pipeline(_cfg(work, git_repo))
    result = run_pipeline(_cfg(work, git_repo))
    print(f"\n[test input] repeat run -> actions="
          f"{[b.action for b in preview.builds]}")
    assert all(b.action == "reuse" for b in preview.builds)
    _assert_agrees(preview, result)


def test_preview_matches_blocked_rollout(tmp_path, git_repo):
    work = tmp_path / "w"
    policy = {"canary": (False, True)}
    preview = preview_pipeline(_cfg(work, git_repo, health_policy=policy))
    result = run_pipeline(_cfg(work, git_repo, health_policy=policy))
    print(f"\n[test input] unhealthy canary -> preview blocked="
          f"{[(d.target, d.blocked_at) for d in preview.deploys]} "
          f"exit={result.exit_code}")
    assert preview.predicted_exit_code == EXIT_ROLLOUT_BLOCKED
    assert all(d.blocked_at == "canary" for d in preview.deploys)
    assert all(d.final_stage == "staging" for d in preview.deploys)
    _assert_agrees(preview, result)


def test_preview_matches_partial_build_failure(tmp_path, git_repo):
    work = tmp_path / "w"
    over = dict(
        targets=("linux-x86_64", "linux-aarch64"),
        failing_targets=frozenset({"linux-aarch64"}),
    )
    preview = preview_pipeline(_cfg(work, git_repo, **over))
    result = run_pipeline(_cfg(work, git_repo, **over))
    print(f"\n[test input] failing linux-aarch64 -> preview actions="
          f"{[(b.target, b.action) for b in preview.builds]}")
    assert preview.outcome == "partial-failure"
    # The failed target has no deploy plan in either world.
    assert "linux-aarch64" not in {d.target for d in preview.deploys}
    assert "linux-aarch64" not in {
        o.target for o in result.target_outcomes if o.final_stage
    }
    _assert_agrees(preview, result)


def test_preview_reflects_state_progression(tmp_path, git_repo):
    work = tmp_path / "w"
    # Advance only to staging first, then preview the remaining chain.
    run_pipeline(_cfg(work, git_repo, targets=("linux-x86_64",),
                      stages=("staging",)))
    preview = preview_pipeline(_cfg(work, git_repo,
                                  targets=("linux-x86_64",)))
    print(f"\n[test input] staged state -> current="
          f"{preview.deploys[0].current_stage} planned="
          f"{preview.deploys[0].planned_stages}")
    assert preview.deploys[0].current_stage == "staging"
    assert preview.deploys[0].planned_stages == ("canary", "full")
    result = run_pipeline(_cfg(work, git_repo, targets=("linux-x86_64",)))
    _assert_agrees(preview, result)


# --------------------------------------------------------------- concurrency
def test_preview_concurrent_with_execution(tmp_path, git_repo):
    work = tmp_path / "w"
    cfg = _cfg(work, git_repo, targets=("linux-x86_64",))
    run_pipeline(cfg)  # populate: steady state, executions are idempotent
    before = _snapshot(work)
    errors: list[Exception] = []
    previews = []
    runs = []

    def do_preview():
        try:
            previews.append(preview_pipeline(cfg))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def do_run():
        try:
            runs.append(run_pipeline(cfg))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = ([threading.Thread(target=do_preview) for _ in range(4)]
               + [threading.Thread(target=do_run) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"\n[test input] 4 previews + 2 runs concurrent -> "
          f"errors={errors} previews={len(previews)}")
    assert not errors
    # Shared state is intact and every run still converges.
    assert all(r.exit_code == EXIT_OK for r in runs)
    # Previews never corrupted anything the runs did not themselves write.
    after = _snapshot(work)
    assert set(before) <= set(after)
    # Every preview agrees with a real execution at steady state.
    steady = run_pipeline(cfg)
    for p in previews:
        _assert_agrees(p, steady)


# --------------------------------------------------------------- determinism
def test_preview_stable_across_cwd_env_and_mode(tmp_path, git_repo, monkeypatch):
    work = tmp_path / "w"
    run_pipeline(_cfg(work, git_repo))
    kw = dict(env={"PATH": "/usr/bin", "CC": "cc"}, config={"opt": True})
    base = preview_pipeline(_cfg(work, git_repo, **kw))

    monkeypatch.chdir(tmp_path)  # different working directory
    monkeypatch.setenv("CI_RUN_ID", "12345")  # irrelevant ambient noise
    monkeypatch.setenv("HOME", "/nonexistent-home")
    local = preview_pipeline(_cfg(work, git_repo, **kw))
    ci = preview_pipeline(_cfg(work, git_repo, mode="ci", **kw))

    def _norm(p):
        return (
            p.version, p.predicted_exit_code, p.outcome,
            tuple((b.target, b.action, b.cache_key, b.content_hash)
                  for b in p.builds),
            tuple((d.target, d.current_stage, d.planned_stages,
                   d.final_stage, d.blocked_at) for d in p.deploys),
        )

    print(f"\n[test input] cwd/env/mode varied -> equal="
          f"{_norm(base) == _norm(local) == _norm(ci)}")
    assert _norm(base) == _norm(local) == _norm(ci)


def test_preview_corrupt_cache_matches_rebuild_execution(tmp_path, git_repo):
    work = tmp_path / "w"
    over = dict(targets=("linux-x86_64",))
    run_pipeline(_cfg(work, git_repo, **over))
    victims = list((work / "cache").rglob("artifact.bin"))
    victims[0].write_bytes(b"tampered")
    # Delete state/artifacts too so the run really rebuilds+redeploys and the
    # comparison is meaningful; the preview still predicts rebuild + content.
    import shutil
    shutil.rmtree(work / "artifacts")
    preview = preview_pipeline(_cfg(work, git_repo, **over))
    assert preview.builds[0].action == "rebuild"
    result = run_pipeline(_cfg(work, git_repo, **over))
    print(f"\n[test input] corrupt cache rebuild -> predicted hash == "
          f"actual: {preview.builds[0].content_hash == result.build.results[0].artifact.content_hash}")
    _assert_agrees(preview, result)
