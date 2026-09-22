"""Build matrix: isolation, partial failure, cache reuse, invalidation."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_pipeline.builder import (
    fingerprint_config,
    fingerprint_environment,
    run_matrix,
)
from release_pipeline.cache import BuildCache
from release_pipeline.models import BuildTarget, TargetStatus, VersionInfo

pytestmark = pytest.mark.logging_inputs


@pytest.fixture
def version():
    return VersionInfo(
        version="1.0.0+2.gabcdef0",
        commit="abcdef0",
        dirty=False,
        commit_count=2,
        source="git-tag",
        reproducible_hash="h" * 16,
    )


@pytest.fixture
def targets():
    return [
        BuildTarget("t1", "app-t1.bin"),
        BuildTarget("t2", "app-t2.bin", fail=True),
        BuildTarget("t3", "app-t3.bin"),
    ]


def test_partial_failure_is_isolated(tmp_path, targets, version):
    cache = BuildCache(tmp_path / "cache")
    report = run_matrix(
        targets, version, cache, str(tmp_path / "art"),
        env_fingerprint="E", config_fingerprint="C", source_hash="S",
    )
    by = {r.target: r for r in report.results}
    print(f"\n[test input] matrix={[t.name + ('(fail)' if t.fail else '') for t in targets]}")
    for r in report.results:
        print(f"  -> target={r.target} status={r.status.value} reason={r.reason}")
    assert by["t1"].status is TargetStatus.SUCCESS
    assert by["t3"].status is TargetStatus.SUCCESS
    assert by["t2"].status is TargetStatus.FAILED
    assert by["t2"].artifact is None
    assert "build-error" in by["t2"].reason
    assert not report.all_succeeded
    # Successful artifacts exist; the failed target never publishes one.
    assert (tmp_path / "art" / "app-t1.bin").exists()
    assert (tmp_path / "art" / "app-t3.bin").exists()
    assert not (tmp_path / "art" / "app-t2.bin").exists()
    # No scratch directories leak.
    assert not list((tmp_path / "art").glob(".scratch-*"))


def test_rerun_uses_cache_and_preserves_failure(tmp_path, targets, version):
    cache = BuildCache(tmp_path / "cache")
    kw = dict(env_fingerprint="E", config_fingerprint="C", source_hash="S")
    run_matrix(targets, version, cache, str(tmp_path / "a1"), **kw)
    report = run_matrix(targets, version, cache, str(tmp_path / "a2"), **kw)
    by = {r.target: r for r in report.results}
    print("\n[test input] identical rerun -> "
          f"t1={by['t1'].reason} t2={by['t2'].status.value} t3={by['t3'].reason}")
    assert by["t1"].reason == "cache-hit"
    assert by["t3"].reason == "cache-hit"
    assert by["t2"].status is TargetStatus.FAILED, \
        "a failed target must not be reported as successful on rerun"


def test_environment_change_invalidates_cache(tmp_path, version):
    cache = BuildCache(tmp_path / "cache")
    t = [BuildTarget("t1", "app-t1.bin")]
    r1 = run_matrix(t, version, cache, str(tmp_path / "a1"),
                    env_fingerprint="E1", config_fingerprint="C", source_hash="S")
    r2 = run_matrix(t, version, cache, str(tmp_path / "a2"),
                    env_fingerprint="E2", config_fingerprint="C", source_hash="S")
    print(f"\n[test input] env E1->E2: {r1.results[0].reason} then {r2.results[0].reason}")
    assert r1.results[0].reason == "built"
    assert r2.results[0].reason == "built", "env change must not hit stale cache"


def test_config_and_source_invalidate_cache(tmp_path, version):
    cache = BuildCache(tmp_path / "cache")
    t = [BuildTarget("t1", "app-t1.bin")]
    base = dict(env_fingerprint="E", source_hash="S")
    r_cfg1 = run_matrix(t, version, cache, str(tmp_path / "c1"),
                        config_fingerprint="C1", **base)
    r_cfg2 = run_matrix(t, version, cache, str(tmp_path / "c2"),
                        config_fingerprint="C2", **base)
    r_src2 = run_matrix(t, version, cache, str(tmp_path / "s2"),
                        config_fingerprint="C1", env_fingerprint="E",
                        source_hash="S2")
    print(f"\n[test input] cfg1={r_cfg1.results[0].reason} "
          f"cfg2={r_cfg2.results[0].reason} src2={r_src2.results[0].reason}")
    assert r_cfg2.results[0].reason == "built"
    assert r_src2.results[0].reason == "built"


def test_explicit_env_fingerprint_is_mode_independent(monkeypatch):
    # Ambient variables must not change an explicitly supplied fingerprint
    # basis (this is what local/CI parity relies on).
    monkeypatch.setenv("NOISE_VAR", "x")
    fp1 = fingerprint_environment({"PATH": "/usr/bin"})
    monkeypatch.setenv("NOISE_VAR", "y")
    fp2 = fingerprint_environment({"PATH": "/usr/bin"})
    assert fp1 == fp2
    fp3 = fingerprint_environment({"PATH": "/elsewhere"})
    assert fp3 != fp1
    assert fingerprint_config({"a": 1, "b": 2}) == fingerprint_config({"b": 2, "a": 1})


def test_duplicate_target_names_rejected(tmp_path, version):
    cache = BuildCache(tmp_path / "cache")
    dup = [BuildTarget("t", "a.bin"), BuildTarget("t", "b.bin")]
    with pytest.raises(ValueError):
        run_matrix(dup, version, cache, str(tmp_path / "a"),
                   env_fingerprint="E", config_fingerprint="C", source_hash="S")
