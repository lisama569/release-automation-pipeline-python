"""缓存复用：键覆盖、环境变化失效、损坏降级重建、并发安全。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_pipeline.builder import build_target, digest_spec, run_matrix
from release_pipeline.cache import (
    BuildCache,
    compute_cache_key,
    digest_environment,
    digest_paths,
)
from release_pipeline.locking import read_json
from release_pipeline.models import BuildSpec, CacheStatus, MatrixConclusion, TargetStatus


def _spec(command: str = "sim:ok", target: str = "t1", **options) -> BuildSpec:
    return BuildSpec(
        target=target,
        command=command,
        inputs=("src.txt",),
        options=tuple(sorted(options.items())),
    )


def test_cache_hit_on_repeat_build(tmp_path: Path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.txt").write_text("data", encoding="utf-8")
    cache = BuildCache(tmp_path / "cache")
    spec = _spec()
    r1 = build_target(spec, version="1.0.0", repo_root=repo,
                      work_root=tmp_path / "work", cache=cache,
                      env_digest="e", sources_digest="s")
    r2 = build_target(spec, version="1.0.0", repo_root=repo,
                      work_root=tmp_path / "work2", cache=cache,
                      env_digest="e", sources_digest="s")
    assert r1.status is TargetStatus.SUCCESS and r1.cache_status is CacheStatus.MISS
    assert r2.status is TargetStatus.SUCCESS and r2.cache_status is CacheStatus.HIT
    assert r1.artifact_digest == r2.artifact_digest, "命中缓存必须复用同一产物摘要"
    assert "判定=HIT" in caplog.text


def test_cache_invalidates_on_env_change(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.txt").write_text("data", encoding="utf-8")
    cache = BuildCache(tmp_path / "cache")
    spec = _spec()
    r1 = build_target(spec, version="1.0.0", repo_root=repo, work_root=tmp_path / "w1",
                      cache=cache, env_digest="env-A", sources_digest="s")
    r2 = build_target(spec, version="1.0.0", repo_root=repo, work_root=tmp_path / "w2",
                      cache=cache, env_digest="env-B", sources_digest="s")
    assert r1.cache_status is CacheStatus.MISS and r2.cache_status is CacheStatus.MISS, (
        "环境摘要变化后不得命中过期缓存"
    )
    assert r1.cache_key != r2.cache_key


def test_cache_invalidates_on_source_change(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src.txt"
    src.write_text("v1", encoding="utf-8")
    d1 = digest_paths([src], base=repo)
    src.write_text("v2", encoding="utf-8")
    d2 = digest_paths([src], base=repo)
    assert d1 != d2, "输入内容变化必须改变源摘要"

    cache = BuildCache(tmp_path / "cache")
    spec = _spec()
    r1 = build_target(spec, version="1.0.0", repo_root=repo, work_root=tmp_path / "w1",
                      cache=cache, env_digest="e", sources_digest=d1)
    r2 = build_target(spec, version="1.0.0", repo_root=repo, work_root=tmp_path / "w2",
                      cache=cache, env_digest="e", sources_digest=d2)
    assert r2.cache_status is CacheStatus.MISS


def test_cache_invalidates_on_command_change(tmp_path: Path):
    s1 = _spec("sim:ok")
    s2 = _spec("sim:ok", flag="x")
    assert digest_spec(s1) != digest_spec(s2), "构建选项变化必须改变规格摘要"
    s3 = BuildSpec(target="t1", command="sim:fail:build")
    assert digest_spec(s1) != digest_spec(s3), "构建命令变化必须改变规格摘要"


def test_cache_key_covers_all_inputs():
    base = compute_cache_key(target="t", version="1", spec_digest="a",
                             sources_digest="b", env_digest="c")
    assert compute_cache_key(target="t2", version="1", spec_digest="a",
                             sources_digest="b", env_digest="c") != base
    assert compute_cache_key(target="t", version="2", spec_digest="a",
                             sources_digest="b", env_digest="c") != base
    assert compute_cache_key(target="t", version="1", spec_digest="a2",
                             sources_digest="b", env_digest="c") != base
    assert compute_cache_key(target="t", version="1", spec_digest="a",
                             sources_digest="b2", env_digest="c") != base
    assert compute_cache_key(target="t", version="1", spec_digest="a",
                             sources_digest="b", env_digest="c2") != base


def test_corrupt_metadata_rebuilds(tmp_path: Path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.txt").write_text("data", encoding="utf-8")
    cache = BuildCache(tmp_path / "cache")
    spec = _spec()
    r1 = build_target(spec, version="1.0.0", repo_root=repo, work_root=tmp_path / "w1",
                      cache=cache, env_digest="e", sources_digest="s")
    # 破坏元数据
    entry_path = cache.root / "entries" / f"{r1.cache_key}.json"
    entry_path.write_text("{ broken json", encoding="utf-8")
    r2 = build_target(spec, version="1.0.0", repo_root=repo, work_root=tmp_path / "w2",
                      cache=cache, env_digest="e", sources_digest="s")
    assert r2.status is TargetStatus.SUCCESS, "缓存损坏必须降级重建而不是失败"
    assert r2.cache_status is CacheStatus.CORRUPT_REBUILT
    assert r2.artifact_digest == r1.artifact_digest, "重建产物必须与原产物一致（同输入）"
    assert "CORRUPT" in caplog.text
    assert (cache.quarantine_dir).exists()


def test_corrupt_artifact_digest_rebuilds(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.txt").write_text("data", encoding="utf-8")
    cache = BuildCache(tmp_path / "cache")
    spec = _spec()
    r1 = build_target(spec, version="1.0.0", repo_root=repo, work_root=tmp_path / "w1",
                      cache=cache, env_digest="e", sources_digest="s")
    artifact = cache.root / "artifacts" / f"{r1.cache_key}.bin"
    artifact.write_bytes(b"tampered")
    r2 = build_target(spec, version="1.0.0", repo_root=repo, work_root=tmp_path / "w2",
                      cache=cache, env_digest="e", sources_digest="s")
    assert r2.cache_status is CacheStatus.CORRUPT_REBUILT
    assert r2.artifact_digest == r1.artifact_digest


def test_env_digest_whitelist_only():
    d1 = digest_environment({"RELEASE_TARGET_ARCH": "x64"}, allowed=["RELEASE_TARGET_ARCH"])
    d2 = digest_environment({"RELEASE_TARGET_ARCH": "arm64"}, allowed=["RELEASE_TARGET_ARCH"])
    d3 = digest_environment({"RELEASE_TARGET_ARCH": "x64", "IRRELEVANT": "zz"},
                            allowed=["RELEASE_TARGET_ARCH"])
    assert d1 != d2
    assert d1 == d3, "白名单外变量不得影响环境摘要"


def test_missing_declared_input_fails_fast(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = BuildSpec(target="t", command="sim:ok", inputs=("nope.txt",))
    with pytest.raises(FileNotFoundError):
        digest_paths([Path("nope.txt")], base=repo)


def test_concurrent_same_target_single_cache_entry(tmp_path: Path):
    """并发构建同一目标：缓存目录中只允许一个条目，且都成功。"""
    from concurrent.futures import ThreadPoolExecutor

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.txt").write_text("data", encoding="utf-8")
    spec = _spec()

    def one(i: int):
        cache = BuildCache(tmp_path / "cache")
        return build_target(spec, version="1.0.0", repo_root=repo,
                            work_root=tmp_path / f"w{i}", cache=cache,
                            env_digest="e", sources_digest="s")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(8)))
    assert all(r.status is TargetStatus.SUCCESS for r in results)
    digests = {r.artifact_digest for r in results}
    assert len(digests) == 1, f"并发产物摘要不一致: {digests}"
    entries = list((tmp_path / "cache" / "entries").glob("*.json"))
    assert len(entries) == 1, f"并发写入产生多个条目: {entries}"


def test_lookup_status_is_per_call_not_shared_state(tmp_path: Path):
    """回归：多线程共享 cache 对象时，hit/miss 判定不得互相串改。"""
    from concurrent.futures import ThreadPoolExecutor

    cache = BuildCache(tmp_path / "cache")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.txt").write_text("data", encoding="utf-8")
    # 先构建 t-hit（写入缓存），t-miss 不构建
    hit_spec = _spec(target="t-hit")
    hit = build_target(hit_spec, version="1.0.0", repo_root=repo,
                       work_root=tmp_path / "w0", cache=cache,
                       env_digest="e", sources_digest="s")
    miss_spec = _spec(target="t-miss")
    miss = build_target(miss_spec, version="1.0.0", repo_root=repo,
                        work_root=tmp_path / "w1", cache=cache,
                        env_digest="e", sources_digest="s")
    assert hit.cache_status is CacheStatus.MISS
    assert miss.cache_status is CacheStatus.MISS

    # 再次并发查询：t-hit 应稳定 HIT，新键 t-other 稳定 MISS，重复交错多次
    keys = [hit.cache_key] * 20 + [compute_cache_key(
        target="t-other", version="1.0.0", spec_digest="x",
        sources_digest="s", env_digest="e")] * 20

    def query(key: str):
        _, status = cache.lookup(key)
        return key, status

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(query, keys))
    for key, status in outcomes:
        if key == hit.cache_key:
            assert status is CacheStatus.HIT, f"已存在键被错判为 {status}"
        else:
            assert status is CacheStatus.MISS, f"不存在键被错判为 {status}"


def test_matrix_partial_failure_is_isolated(tmp_path: Path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.txt").write_text("data", encoding="utf-8")
    specs = [
        _spec("sim:ok", target="good-a"),
        _spec("sim:fail:build", target="bad"),
        _spec("sim:ok", target="good-b"),
    ]
    report = run_matrix(specs, version="1.0.0", repo_root=repo,
                        work_root=tmp_path / "work", cache=None,
                        env_digest="e", sources_digest="s")
    assert report.conclusion is MatrixConclusion.PARTIAL_FAILURE
    assert set(report.successful) == {"good-a", "good-b"}
    assert report.failed == ("bad",)
    for r in report.results:
        if r.target == "bad":
            assert r.status is TargetStatus.FAILED and r.artifact_path is None
        else:
            assert r.status is TargetStatus.SUCCESS and r.artifact_path is not None
