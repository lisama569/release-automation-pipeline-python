"""版本推导：重复执行一致性、历史缺失降级、来源冲突、dirty 区分。"""

from __future__ import annotations

from pathlib import Path

import pytest

from release_pipeline.errors import VersionError
from release_pipeline.versioning import derive_version, normalize_version
from tests.conftest import assert_log_contains, git


def test_version_reproducible_across_runs(git_repo: Path, caplog):
    v1 = derive_version(git_repo)
    v2 = derive_version(git_repo)
    assert v1.version == v2.version, "同一仓库重复推导必须得到同一版本"
    assert v1.commit == v2.commit
    assert_log_contains(caplog, "version=")


def test_version_based_on_base_tag_and_commit_count(git_repo_tagged: Path):
    v = derive_version(git_repo_tagged)
    # v1.0.0 之后 1 个提交，且工作区干净 => 1.0.0.dev1
    assert v.version == "1.0.0.dev1", f"实际 {v.version}"
    assert v.source == "git-tag"


def test_tag_on_head_is_release_version(git_repo_tagged: Path):
    git(git_repo_tagged, "tag", "v1.1.0", "HEAD")
    v = derive_version(git_repo_tagged)
    assert v.version == "1.1.0", f"HEAD 命中标签应为发布版本，实际 {v.version}"


def test_no_tags_no_history_falls_back_deterministically(tmp_path, caplog):
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "f.txt").write_text("hello", encoding="utf-8")
    # 不提交：unborn HEAD，且有未跟踪文件 => 同时带 dirty 与 tree 摘要
    v1 = derive_version(repo)
    v2 = derive_version(repo)
    assert v1.version == v2.version, "无历史时也必须可复现"
    assert v1.version.startswith("0.0.0.dev0+dirty."), v1.version
    assert ".tree." in v1.version
    assert "历史信息缺失" in v1.resolution


def test_dirty_working_tree_changes_version(git_repo: Path):
    clean = derive_version(git_repo).version
    (git_repo / "app.py").write_text('print("changed")\n', encoding="utf-8")
    dirty = derive_version(git_repo).version
    assert dirty != clean, "工作区改动必须体现在版本中"
    assert "+dirty." in dirty


def test_different_dirty_content_gets_different_version(git_repo: Path):
    (git_repo / "app.py").write_text("a\n", encoding="utf-8")
    va = derive_version(git_repo).version
    (git_repo / "app.py").write_text("bb\n", encoding="utf-8")
    vb = derive_version(git_repo).version
    assert va != vb, "不同脏内容必须产生不同版本"


def test_explicit_version_wins_over_tag_and_records_conflict(git_repo_tagged: Path):
    v = derive_version(git_repo_tagged, explicit_version="9.9.9")
    assert v.version.startswith("9.9.9"), v.version
    assert "来源冲突" in v.resolution, "冲突必须显式记录，不得静默"


def test_invalid_explicit_version_rejected(git_repo: Path):
    for bad in ("", "1.2", "latest", "1.2.3; rm -rf /", "v2"):
        with pytest.raises(VersionError):
            derive_version(git_repo, explicit_version=bad)


def test_version_file_source_and_conflict(git_repo_tagged: Path):
    (git_repo_tagged / "VERSION").write_text("2.0.0\n", encoding="utf-8")
    v = derive_version(git_repo_tagged)
    assert v.version.startswith("2.0.0")
    assert v.source == "file:VERSION"
    assert "来源冲突" in v.resolution


def test_changelog_includes_commits_and_baseline(git_repo_tagged: Path):
    v = derive_version(git_repo_tagged)
    joined = "\n".join(v.changelog)
    assert "基线标签: v1.0.0" in joined
    assert "feat: iteration 2" in joined


def test_non_git_directory_degrades_with_explanation(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x", encoding="utf-8")
    v = derive_version(plain)
    assert v.version.startswith("0.0.0.dev0+tree."), v.version
    assert v.commit is None
    assert "git 不可用" in "\n".join(v.changelog)
    # 内容不同 => tree 摘要不同
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.txt").write_text("y", encoding="utf-8")
    assert derive_version(other).version != v.version


def test_normalize_version_variants():
    assert normalize_version("v1.2.3") == "1.2.3"
    assert normalize_version(" 1.2.3 ") == "1.2.3"
    assert normalize_version("1.2.3.dev2") == "1.2.3.dev2"
    with pytest.raises(VersionError):
        normalize_version("not-a-version")
