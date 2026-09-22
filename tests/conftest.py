"""pytest 公共夹具与日志配置：单测日志打印关键输入与判定依据。"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest


def _git_env() -> dict[str, str]:
    import os

    env = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Pipeline Test",
        "GIT_AUTHOR_EMAIL": "pipeline-test@example.com",
        "GIT_COMMITTER_NAME": "Pipeline Test",
        "GIT_COMMITTER_EMAIL": "pipeline-test@example.com",
    }
    for key in ("SYSTEMROOT",):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {proc.stderr}")
    return proc


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """带 3 个提交的干净仓库。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for i in range(3):
        (repo / "app.py").write_text(f"print({i})\n", encoding="utf-8")
        git(repo, "add", "app.py")
        git(repo, "commit", "-q", "-m", f"feat: iteration {i}")
    return repo


@pytest.fixture
def git_repo_tagged(git_repo: Path) -> Path:
    """在第二个提交打 v1.0.0，之后再追加一个提交。"""
    commits = git(git_repo, "log", "--pretty=%H").stdout.split()
    git(git_repo, "tag", "v1.0.0", commits[-2])  # 倒数第二个提交
    return git_repo


@pytest.fixture(autouse=True)
def _capture_pipeline_logs(caplog: pytest.LogCaptureFixture) -> None:
    """所有用例自动收集 INFO 日志，失败时可直接看到关键输入与判定依据。"""
    caplog.set_level(logging.INFO, logger="release_pipeline")


def assert_log_contains(caplog: pytest.LogCaptureFixture, needle: str) -> None:
    """断言日志中出现关键判定依据，并给出可区分的失败原因。"""
    text = "\n".join(f"{r.levelname} {r.name}: {r.message}" for r in caplog.records)
    assert needle in text, (
        f"期望日志包含 {needle!r}，实际日志:\n{text or '<空>'}"
    )
