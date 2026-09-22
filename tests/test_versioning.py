"""Version derivation: determinism, fallbacks, conflicts, changelog."""

from __future__ import annotations

import os
import subprocess

import pytest

from release_pipeline.errors import VersionConflictError, VersionSourceError
from release_pipeline.versioning import (
    UNKNOWN_SHA,
    build_changelog,
    derive_version,
    is_dirty,
    read_commits,
)
from tests.conftest import _git

pytestmark = pytest.mark.logging_inputs


def test_repeated_derivation_is_identical(git_repo):
    v1 = derive_version(str(git_repo))
    v2 = derive_version(str(git_repo))
    print(f"\n[test input] repo={git_repo} commits={v1.commit_count} "
          f"dirty={v1.dirty} -> version={v1.version}")
    assert v1 == v2, "same repo state must produce identical versions"
    assert v1.version.startswith("1.0.0+2.g"), v1.version
    assert not v1.dirty
    assert v1.commit_count == 2


def test_changelog_newest_first(git_repo):
    commits = read_commits(str(git_repo))
    log = build_changelog(commits)
    print(f"\n[test input] {len(log)} commits; head={log[0]}")
    assert [s for s, _ in log] == [c.short_sha for c in commits]
    assert log[0][1] == "second commit"
    assert log[1][1] == "first commit"


def test_dirty_tree_changes_version_deterministically(git_repo):
    clean = derive_version(str(git_repo))
    (git_repo / "untracked.txt").write_text("x")
    dirty1 = derive_version(str(git_repo))
    dirty2 = derive_version(str(git_repo))
    print(f"\n[test input] clean={clean.version} dirty={dirty1.version}")
    assert dirty1.dirty
    assert ".dirty." in dirty1.version
    assert dirty1 == dirty2, "dirty derivation must be repeatable"
    assert dirty1.version != clean.version
    assert dirty1.commit == clean.commit
    os.remove(git_repo / "untracked.txt")
    assert derive_version(str(git_repo)) == clean


def test_missing_git_history_uses_documented_fallback(empty_dir):
    v = derive_version(str(empty_dir))
    print(f"\n[test input] non-git dir -> {v.version} source={v.source}")
    assert v.version == f"0.0.0+0.g{UNKNOWN_SHA}"
    assert v.source == "fallback-no-git"
    assert v.commit == UNKNOWN_SHA
    assert v.commit_count == 0
    assert derive_version(str(empty_dir)) == v


def test_declared_version_matching_tag_is_accepted(git_repo):
    v = derive_version(str(git_repo), declared="1.0.0")
    print(f"\n[test input] declared=1.0.0 -> {v.version} source={v.source}")
    assert v.version.startswith("1.0.0+2.g")
    assert v.source.startswith("declared+")


def test_declared_version_conflicting_tag_is_rejected(git_repo):
    with pytest.raises(VersionConflictError) as exc:
        derive_version(str(git_repo), declared="2.0.0")
    print(f"\n[test input] declared=2.0.0 vs tag v1.0.0 -> {exc.value.code}")
    assert exc.value.code == "version_conflict"


def test_declared_fallback_does_not_fabricate_conflict(empty_dir):
    # No git tag exists; a declared version must not be called a conflict.
    v = derive_version(str(empty_dir), declared="3.4.5")
    assert v.version.startswith("3.4.5+0.g0000000")


def test_bad_declared_version_raises(git_repo):
    with pytest.raises(VersionSourceError) as exc:
        derive_version(str(git_repo), declared="not-a-version")
    assert exc.value.code == "bad_declared_version"


def test_version_independent_of_ambient_env(git_repo, monkeypatch):
    # Random ambient variables must not leak into the version.
    monkeypatch.setenv("RANDOM_TOOL_VAR", "zzz")
    monkeypatch.setenv("CI_RUN_ID", "abc123")
    v1 = derive_version(str(git_repo))
    monkeypatch.delenv("RANDOM_TOOL_VAR", raising=False)
    monkeypatch.delenv("CI_RUN_ID", raising=False)
    v2 = derive_version(str(git_repo))
    print(f"\n[test input] with/without env noise: {v1.version} == {v2.version}")
    assert v1 == v2


def test_different_history_different_version(git_repo):
    before = derive_version(str(git_repo))
    _git(git_repo, "commit", "--allow-empty", "-qm", "third commit")
    after = derive_version(str(git_repo))
    print(f"\n[test input] {before.version} -> {after.version}")
    assert after.version != before.version
    assert after.commit_count == 3
