"""Shared fixtures: isolated git repositories and work dirs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
    }
    proc = subprocess.run(
        ["git", *args], cwd=repo, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh tagged git repo with two deterministic commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("first\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first commit")
    _git(repo, "tag", "v1.0.0")
    (repo / "b.txt").write_text("second\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "second commit")
    return repo


@pytest.fixture
def empty_dir(tmp_path: Path) -> Path:
    d = tmp_path / "nongit"
    d.mkdir()
    return d
