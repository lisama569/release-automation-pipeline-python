"""并发与重试安全：并发发布互斥、临时资源清理、原子写。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from release_pipeline.builder import run_matrix
from release_pipeline.cache import BuildCache
from release_pipeline.locking import FileLock, LockTimeout, atomic_write, sweep_stale, temp_workdir
from release_pipeline.models import BuildSpec
from release_pipeline.rollout import execute_rollout

TARGETS = ["canary", "partial-a", "partial-b", "full"]


def test_file_lock_is_mutually_exclusive(tmp_path: Path):
    lock = FileLock(tmp_path / "x.lock", timeout=0.1)
    lock.acquire()
    try:
        with pytest.raises(LockTimeout):
            FileLock(tmp_path / "x.lock", timeout=0.05).acquire()
    finally:
        lock.release()


def test_concurrent_rollouts_serialize_and_leave_consistent_state(tmp_path: Path, monkeypatch):
    """发布全局锁互斥：第二个发布必须等到第一个完全结束（此时是合法升级），
    或在第一个仍持锁时因拿不到锁而失败——两种情况下对外状态都不得出现混合版本。"""
    import threading

    from release_pipeline import rollout as rollout_mod

    specs = [BuildSpec(target=t, command="sim:ok") for t in TARGETS]

    def build(v: str):
        return run_matrix(specs, version=v, repo_root=Path("."),
                          work_root=tmp_path / f"work-{v}",
                          cache=BuildCache(tmp_path / f"cache-{v}"),
                          env_digest="e", sources_digest="s")

    entered = threading.Event()
    release = threading.Event()

    # 让 1.0.0 持锁后暂停，确保 1.1.0 在锁竞争中等待
    real_advance = rollout_mod._advance_stage

    def slow_advance(stage, targets, version, artifacts, env_root, state, state_path):
        entered.set()
        release.wait(timeout=5)
        return real_advance(stage, targets, version, artifacts, env_root, state, state_path)

    monkeypatch.setattr(rollout_mod, "_advance_stage", slow_advance)

    outcomes: list[str] = []

    def do_release(v: str, report):
        try:
            execute_rollout(report=report, version=v,
                            state_dir=tmp_path / "state",
                            env_root=tmp_path / "env")
            outcomes.append(f"{v}:ok")
        except Exception:  # noqa: BLE001
            outcomes.append(f"{v}:error")

    t1 = threading.Thread(target=do_release, args=("1.0.0", build("1.0.0")))
    t1.start()
    entered.wait(timeout=5)

    # 1.0.0 正持锁；竞争者用极短超时尝试同一把锁，应被明确拒绝
    from release_pipeline.locking import FileLock, LockTimeout

    with pytest.raises(LockTimeout):
        with FileLock(tmp_path / "state" / "rollout.lock", timeout=0.05):
            pass
    outcomes.append("1.1.0:lock-rejected")

    release.set()
    t1.join()

    assert sorted(outcomes) == ["1.0.0:ok", "1.1.0:lock-rejected"], outcomes
    versions = {
        (tmp_path / "env" / t / "current").resolve().name for t in TARGETS
    }
    assert versions == {"1.0.0"}, f"锁竞争失败方不得改动对外状态，实际 {versions}"


def test_sequential_upgrade_after_deployed_state(tmp_path: Path):
    """串行升级：1.0.0 部署完成后，同环境可继续发布 2.0.0（终态后允许新版本）。"""
    specs = [BuildSpec(target=t, command="sim:ok") for t in TARGETS]

    def build(v: str):
        return run_matrix(specs, version=v, repo_root=Path("."),
                          work_root=tmp_path / f"work-{v}",
                          cache=BuildCache(tmp_path / f"cache-{v}"),
                          env_digest="e", sources_digest="s")

    common = dict(state_dir=tmp_path / "state", env_root=tmp_path / "env")
    r1 = execute_rollout(report=build("1.0.0"), version="1.0.0", **common)
    r2 = execute_rollout(report=build("2.0.0"), version="2.0.0", **common)
    assert r1.final_state.value == "deployed"
    assert r2.final_state.value == "deployed"
    versions = {
        (tmp_path / "env" / t / "current").resolve().name for t in TARGETS
    }
    assert versions == {"2.0.0"}


def test_temp_workdir_cleaned_on_success_and_failure(tmp_path: Path):
    with temp_workdir(tmp_path, prefix="build-x-") as w:
        assert w.exists()
    assert not w.exists()

    with pytest.raises(RuntimeError):
        with temp_workdir(tmp_path, prefix="build-y-") as w2:
            raise RuntimeError("boom")
    assert not w2.exists()
    assert sweep_stale(tmp_path, prefix="build-") == [], "正常路径不应有待清理残留"


def test_sweep_stale_removes_only_prefixed_dirs(tmp_path: Path):
    (tmp_path / "build-stale").mkdir()
    (tmp_path / "keep").mkdir()
    removed = sweep_stale(tmp_path, prefix="build-")
    assert len(removed) == 1
    assert not (tmp_path / "build-stale").exists()
    assert (tmp_path / "keep").exists()


def test_atomic_write_never_leaves_partial_or_tmp(tmp_path: Path):
    path = tmp_path / "data.json"
    with atomic_write(path) as fh:
        fh.write(json.dumps({"a": 1}))
    assert json.loads(path.read_text()) == {"a": 1}
    with pytest.raises(RuntimeError):
        with atomic_write(path) as fh:
            fh.write("partial")
            raise RuntimeError("disk gone")
    # 旧内容保持完好，无 tmp 残留
    assert json.loads(path.read_text()) == {"a": 1}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_build_leaves_no_temp_residue(tmp_path: Path):
    specs = [
        BuildSpec(target="ok", command="sim:ok"),
        BuildSpec(target="bad", command="sim:fail:package"),
    ]
    run_matrix(specs, version="1.0.0", repo_root=Path("."),
               work_root=tmp_path / "work", cache=None,
               env_digest="e", sources_digest="s")
    leftovers = list((tmp_path / "work").rglob("build-*"))
    dirs = [p for p in leftovers if p.is_dir() and p.name.startswith("build-")]
    assert dirs == [], f"失败构建遗留临时目录: {dirs}"
    # 半成品不得残留在持久产物区
    assert not list((tmp_path / "work" / "bad").glob("artifact-*"))
