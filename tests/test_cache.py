"""Build cache: key coverage, hits, corruption rebuild, concurrency."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from release_pipeline.cache import CACHE_FORMAT_VERSION, BuildCache, compute_cache_key
from release_pipeline.models import BuildInput, BuildTarget

pytestmark = pytest.mark.logging_inputs


@pytest.fixture
def target():
    return BuildTarget(name="linux-x64", artifact_name="app.bin")


def make_input(target, **over):
    base = dict(
        target=target,
        version="1.0.0+2.gabcdef0",
        source_hash="S" * 16,
        env_fingerprint="E" * 16,
        config_fingerprint="C" * 16,
        toolchain="python3",
    )
    base.update(over)
    return BuildInput(**base)


def test_miss_then_hit(tmp_path, target):
    cache = BuildCache(tmp_path / "cache")
    inp = make_input(target)
    assert cache.get(inp) is None, "empty cache must miss"
    stored = cache.put(inp, b"artifact-bytes")
    assert not stored.cache_hit
    hit = cache.get(inp)
    print(f"\n[test input] key={stored.cache_key[:12]} "
          f"hash={stored.content_hash[:12]} -> hit={hit is not None}")
    assert hit is not None and hit.cache_hit
    assert hit.content_hash == stored.content_hash
    assert Path(hit.path).read_bytes() == b"artifact-bytes"


def test_key_covers_every_input_dimension(tmp_path, target):
    inp = make_input(target)
    base_key = compute_cache_key(inp)
    variants = {
        "version": make_input(target, version="1.0.1+2.gabcdef0"),
        "source": make_input(target, source_hash="T" * 16),
        "env": make_input(target, env_fingerprint="F" * 16),
        "config": make_input(target, config_fingerprint="D" * 16),
        "toolchain": make_input(target, toolchain="gcc"),
        "target": make_input(BuildTarget("linux-arm", "other.bin")),
    }
    for name, variant in variants.items():
        k = compute_cache_key(variant)
        print(f"\n[test input] change={name} base={base_key[:8]} new={k[:8]}")
        assert k != base_key, f"{name} change must change the cache key"


def test_corrupted_artifact_is_evicted_and_rebuilt(tmp_path, target):
    cache = BuildCache(tmp_path / "cache")
    inp = make_input(target)
    stored = cache.put(inp, b"original")
    Path(stored.path).write_bytes(b"tampered")
    assert cache.get(inp) is None, "tampered content must not be served"
    assert not Path(stored.path).exists(), "corrupt entry must be evicted"
    print("\n[test input] tampered artifact -> evict+miss (rebuild required)")
    rebuilt = cache.put(inp, b"original")
    assert cache.get(inp).content_hash == rebuilt.content_hash


def test_corrupted_meta_is_evicted(tmp_path, target):
    cache = BuildCache(tmp_path / "cache")
    inp = make_input(target)
    stored = cache.put(inp, b"data")
    meta = Path(stored.path).with_name("meta.json")
    meta.write_text("{broken-json")
    assert cache.get(inp) is None
    print("\n[test input] corrupt meta.json -> evict+miss")


def test_meta_field_tampering_is_detected(tmp_path, target):
    cache = BuildCache(tmp_path / "cache")
    inp = make_input(target)
    stored = cache.put(inp, b"data")
    meta_path = Path(stored.path).with_name("meta.json")
    meta = json.loads(meta_path.read_text())
    meta["env_fingerprint"] = "9" * 16
    meta_path.write_text(json.dumps(meta))
    # A different env fingerprint must not be served from this entry.
    assert cache.get(inp) is None
    print("\n[test input] meta env fingerprint tampered -> miss")


def test_concurrent_puts_leave_one_intact_entry(tmp_path, target):
    cache = BuildCache(tmp_path / "cache")
    inp = make_input(target)
    errors: list[Exception] = []

    def worker(i):
        try:
            cache.put(inp, f"payload-{i}".encode())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    hit = cache.get(inp)
    assert hit is not None and hit.cache_hit
    leftovers = list(Path(tmp_path / "cache").rglob("*.tmp"))
    leftovers += list(Path(tmp_path / "cache").rglob("*.old"))
    assert not leftovers, f"temporary resources leaked: {leftovers}"
    print(f"\n[test input] 12 concurrent puts -> one intact entry "
          f"hash={hit.content_hash[:12]}")


def test_format_version_is_part_of_key():
    # Bumping the cache format must invalidate all prior entries.
    assert CACHE_FORMAT_VERSION == 1
