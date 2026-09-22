"""健康探针三态：健康 / 不健康 / 不明确。"""

from __future__ import annotations

import json
from pathlib import Path

from release_pipeline.builder import build_target
from release_pipeline.cache import BuildCache
from release_pipeline.health import probe_target
from release_pipeline.models import BuildSpec
from release_pipeline.targets import LocalTargetEnvironment


def _deploy(tmp_path: Path, health_option: str, *, version: str = "1.0.0"):
    spec = BuildSpec(target="canary", command="sim:ok",
                     options=(("health", health_option),))
    r = build_target(spec, version=version, repo_root=Path("."),
                     work_root=tmp_path / "work", cache=BuildCache(tmp_path / "cache"),
                     env_digest="e", sources_digest="s")
    env = LocalTargetEnvironment(tmp_path / "env", "canary")
    env.stage(version, Path(r.artifact_path))
    env.promote(version)
    return tmp_path / "env"


def test_healthy(tmp_path: Path):
    env_root = _deploy(tmp_path, "ok")
    probe = probe_target(env_root, target="canary", version="1.0.0")
    assert probe.healthy and not probe.ambiguous
    assert "ok" in probe.detail


def test_unhealthy_explicit(tmp_path: Path):
    env_root = _deploy(tmp_path, "fail")
    probe = probe_target(env_root, target="canary", version="1.0.0")
    assert not probe.healthy and not probe.ambiguous
    assert "失败" in probe.detail


def test_ambiguous_when_manifest_missing(tmp_path: Path):
    env_root = _deploy(tmp_path, "ambiguous")
    probe = probe_target(env_root, target="canary", version="1.0.0")
    assert probe.ambiguous and not probe.healthy
    assert "不明确" in probe.detail


def test_ambiguous_when_version_mismatch(tmp_path: Path):
    env_root = _deploy(tmp_path, "ok", version="1.0.0")
    probe = probe_target(env_root, target="canary", version="9.9.9")
    assert probe.ambiguous
    assert "不一致" in probe.detail


def test_ambiguous_when_no_current(tmp_path: Path):
    env = LocalTargetEnvironment(tmp_path / "env", "canary")
    probe = probe_target(tmp_path / "env", target="canary", version="1.0.0")
    assert probe.ambiguous and "无任何版本" in probe.detail


def test_inconsistent_observations_are_ambiguous(tmp_path: Path):
    # 手工构造会在读取间变化的清单
    env = LocalTargetEnvironment(tmp_path / "env", "canary")
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    stage_dir = env.versions_dir / "1.0.0"
    stage_dir.mkdir(parents=True)
    (stage_dir / "payload").write_text("x", encoding="utf-8")
    (stage_dir / "health.json").write_text(
        json.dumps({"status": "ok", "attempts_required": 3, "inconsistent": True}),
        encoding="utf-8",
    )
    env.promote("1.0.0")
    probe = probe_target(tmp_path / "env", target="canary", version="1.0.0")
    assert probe.ambiguous and "矛盾" in probe.detail
