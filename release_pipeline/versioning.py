"""Reproducible version & changelog derivation from repository state.

Version format
--------------
``MAJOR.MINOR.PATCH+<commit_count>.<dirty?>.<short_sha>`` (PEP 440 local
segment), for example ``1.2.3+42.g3f9a2c1`` or
``1.2.3+42.dirty.g3f9a2c1``.

Determinism rules
-----------------
* Same repository state (history + dirty flag) => same version, forever.
* A version declared in project metadata MUST be consistent with the git
  base for that commit; disagreement raises :class:`VersionConflictError`.
* Missing history is not guessed: the commit count falls back to the
  documented sentinel ``0`` and the sha to ``0000000``; the ``source``
  field records exactly which fallback was used.
* Only ``GIT_*`` environment variables are consulted for locating the
  repository, and only variables explicitly on an allow-list influence the
  version; everything else is ignored so local shells and CI agree.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

from .errors import VersionConflictError, VersionSourceError
from .models import CommitInfo, VersionInfo

UNKNOWN_SHA = "0000000"
FALLBACK_BASE = "0.0.0"
# Environment variables that may legitimately alter version derivation.
_GIT_ENV_ALLOWLIST = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_GLOBAL",
        "PATH",
        "SYSTEMROOT",
        "HOME",
        "LANG",
        "LC_ALL",
    }
)


def run_git(repo_dir: str, *args: str) -> str:
    """Run a git command and return stripped stdout.

    Raises :class:`VersionSourceError` if git is missing or exits non-zero.
    """
    env = {k: v for k, v in os.environ.items() if k in _GIT_ENV_ALLOWLIST}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError) as exc:  # pragma: no cover - env dep
        raise VersionSourceError(
            f"git executable unavailable: {exc}", code="git_unavailable"
        ) from exc
    if proc.returncode != 0:
        raise VersionSourceError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}",
            code="git_failed",
        )
    return proc.stdout.strip()


def is_git_repo(repo_dir: str) -> bool:
    try:
        run_git(repo_dir, "rev-parse", "--is-inside-work-tree")
    except VersionSourceError:
        return False
    return True


def read_commits(repo_dir: str, limit: int = 50) -> list[CommitInfo]:
    """Read commit history newest first; empty list if unavailable.

    Uses NUL/record separators so unusual subjects cannot be parsed
    ambiguously and cannot cause nondeterministic splitting.
    """
    if not is_git_repo(repo_dir):
        return []
    try:
        raw = run_git(
            repo_dir,
            "log",
            f"-{limit}",
            "--pretty=format:%H%x1f%h%x1f%s%x1f%ct",
        )
    except VersionSourceError:
        return []
    commits: list[CommitInfo] = []
    if not raw:
        return commits
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:  # corrupt line: never guess, skip deterministically
            continue
        sha, short, subject, ts = parts
        commits.append(
            CommitInfo(
                sha=sha,
                short_sha=short,
                subject=subject,
                commit_time=ts,
            )
        )
    return commits


def is_dirty(repo_dir: str) -> bool:
    """Return whether the working tree has uncommitted changes.

    Untracked-but-ignored files do not count; tracked modifications and
    untracked non-ignored paths do.
    """
    if not is_git_repo(repo_dir):
        return False
    out = run_git(repo_dir, "status", "--porcelain", "--untracked-files=all")
    return bool(out)


def commit_count(repo_dir: str) -> int:
    if not is_git_repo(repo_dir):
        return 0
    try:
        out = run_git(repo_dir, "rev-list", "--count", "HEAD")
    except VersionSourceError:
        return 0
    try:
        return int(out)
    except ValueError:
        return 0


def head_sha(repo_dir: str) -> tuple[str, str]:
    """Return ``(full_sha, short_sha)`` or the documented unknown sentinel."""
    if not is_git_repo(repo_dir):
        return UNKNOWN_SHA, UNKNOWN_SHA
    try:
        full = run_git(repo_dir, "rev-parse", "HEAD")
    except VersionSourceError:
        return UNKNOWN_SHA, UNKNOWN_SHA
    if not full:
        return UNKNOWN_SHA, UNKNOWN_SHA
    return full, full[:7]


def compute_reproducible_hash(
    commits: list[CommitInfo], dirty: bool, source: str
) -> str:
    """Stable 16-char hash over the full version-derivation input.

    The hash makes "same input => same output" auditable and gives callers a
    compact token independent of the formatted version string.
    """
    payload = {
        "dirty": dirty,
        "source": source,
        "commits": [
            [c.sha, c.short_sha, c.subject, c.commit_time] for c in commits
        ],
    }
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def build_changelog(commits: list[CommitInfo]) -> tuple[tuple[str, str], ...]:
    """Return ``(short_sha, subject)`` pairs newest first."""
    return tuple((c.short_sha, c.subject) for c in commits)


def _base_version_from_git(repo_dir: str) -> tuple[str, str]:
    """Determine the PEP440 base version.

    Returns ``(base, source)``. The base is the most recent reachable git
    tag that parses as ``vX.Y.Z``; otherwise :data:`FALLBACK_BASE`.
    """
    if not is_git_repo(repo_dir):
        return FALLBACK_BASE, "fallback-no-git"
    try:
        tag = run_git(
            repo_dir,
            "describe",
            "--tags",
            "--abbrev=0",
            "--match",
            "v[0-9]*.[0-9]*.[0-9]*",
        )
    except VersionSourceError:
        return FALLBACK_BASE, "fallback-no-tag"
    candidate = tag[1:] if tag.startswith("v") else tag
    parts = candidate.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return FALLBACK_BASE, "fallback-unparseable-tag"
    return candidate, "git-tag"


def _normalize_declared(declared: str) -> str:
    base = declared.split("+", 1)[0]
    parts = base.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise VersionSourceError(
            f"declared version {declared!r} is not MAJOR.MINOR.PATCH",
            code="bad_declared_version",
        )
    return base


def derive_version(repo_dir: str, *, declared: str | None = None) -> VersionInfo:
    """Derive a unique, reproducible version for the repository state.

    Repeated calls with the same state always return equal :class:`VersionInfo`
    objects. If ``declared`` is given and disagrees with the git-derived base,
    :class:`VersionConflictError` is raised rather than silently choosing one.
    """
    commits = read_commits(repo_dir)
    dirty = is_dirty(repo_dir)
    count = commit_count(repo_dir)
    full, short = head_sha(repo_dir)
    base, base_source = _base_version_from_git(repo_dir)

    source = base_source
    if declared is not None:
        declared_base = _normalize_declared(declared)
        # Conflict only when git itself provides a concrete, authoritative tag.
        # A "no tag / no git" fallback must not fabricate a disagreement.
        if base_source == "git-tag" and declared_base != base:
            raise VersionConflictError(
                f"declared base {declared_base!s} conflicts with git tag "
                f"base {base!s}; refusing to pick one deterministically"
            )
        base = declared_base
        source = f"declared+{base_source}"

    local_parts = [str(count)]
    if dirty:
        local_parts.append("dirty")
    local_parts.append(f"g{short}")
    version = f"{base}+{'.'.join(local_parts)}"

    return VersionInfo(
        version=version,
        commit=full,
        dirty=dirty,
        commit_count=count,
        source=source,
        reproducible_hash=compute_reproducible_hash(commits, dirty, source),
        changelog=build_changelog(commits),
    )
