"""分阶段上线：健康门、自动回滚、中断重试幂等、并发发布互斥。"""

from __future__ import annotations

from pathlib import Path

import pytest

from release_pipeline.builder import run_matrix
from release_pipeline.cache import BuildCache
from release_pipeline.models import BuildSpec, MatrixConclusion, ReleaseStage
from release_pipeline.rollout import (
    STAGE_PLAN,
    execute_rollout,
    open_target,
    verify_external_state,
)
from release_pipeline.errors import RolloutError

TARGETS = ["canary", "partial-a", "partial-b", "full"]


def _specs(tmp_path: Path, version: str, *, health: dict[str, str] | None = None,
           cache_name: str = "cache", work_name: str = "work"):
    health = health or {}
    specs = [
        BuildSpec(target=t, command="sim:ok",
                  options=(("health", health.get(t, "ok")),))
        for t in TARGETS
    ]
    report = run_matrix(specs, version=version, repo_root=Path("."),
                        work_root=tmp_path / work_name,
                        cache=BuildCache(tmp_path / cache_name),
                        env_digest="e", sources_digest="s")
    return report


def _deploy_baseline(tmp_path: Path, version: str = "0.9.0"):
    report = _specs(tmp_path, version)
    execute_rollout(report=report, version=version,
                    state_dir=tmp_path / f"state-{version}",
                    env_root=tmp_path / "env")


def test_happy_path_full_rollout(tmp_path: Path):
    _deploy_baseline(tmp_path)
    report = _specs(tmp_path, "1.0.0", cache_name="c-new", work_name="w-new")
    rr = execute_rollout(report=report, version="1.0.0",
                         state_dir=tmp_path / "state-new", env_root=tmp_path / "env")
    assert rr.final_state.value == "deployed"
    assert [s.stage for s in rr.stages] == [ReleaseStage.CANARY, ReleaseStage.PARTIAL, ReleaseStage.FULL]
    assert verify_external_state(tmp_path / "env", targets=TARGETS, expected_version="1.0.0")


def test_canary_failure_rolls_back_everything_to_baseline(tmp_path: Path, caplog):
    _deploy_baseline(tmp_path)
    report = _specs(tmp_path, "1.0.0", health={"canary": "fail"},
                    cache_name="c-new", work_name="w-new")
    rr = execute_rollout(report=report, version="1.0.0",
                         state_dir=tmp_path / "state-new", env_root=tmp_path / "env")
    assert rr.rolled_back is True
    assert rr.final_state.value == "rolled_back"
    assert rr.reached_stage is ReleaseStage.CANARY
    assert "健康检查失败" in rr.reason
    for t in TARGETS:
        assert open_target(tmp_path / "env", t).current_version == "0.9.0", (
            f"目标 {t} 回滚后对外版本必须恢复基线"
        )
    assert "自动回滚完成" in caplog.text


def test_ambiguous_health_blocks_and_rolls_back(tmp_path: Path):
    _deploy_baseline(tmp_path)
    report = _specs(tmp_path, "1.0.0", health={"partial-a": "ambiguous"},
                    cache_name="c-new", work_name="w-new")
    rr = execute_rollout(report=report, version="1.0.0",
                         state_dir=tmp_path / "state-new", env_root=tmp_path / "env")
    assert rr.rolled_back is True
    assert "结论不明确" in rr.reason
    for t in TARGETS:
        assert open_target(tmp_path / "env", t).current_version == "0.9.0"


def test_rollback_when_no_prior_version_removes_external_pointer(tmp_path: Path):
    """首次上线即失败：回滚后对外应回到“无版本”，而不是停在新版本。"""
    report = _specs(tmp_path, "1.0.0", health={"canary": "fail"})
    rr = execute_rollout(report=report, version="1.0.0",
                         state_dir=tmp_path / "state", env_root=tmp_path / "env")
    assert rr.rolled_back is True
    for t in TARGETS:
        assert open_target(tmp_path / "env", t).current_version is None


def test_retry_after_success_is_idempotent(tmp_path: Path):
    report = _specs(tmp_path, "1.0.0")
    common = dict(state_dir=tmp_path / "state", env_root=tmp_path / "env")
    rr1 = execute_rollout(report=report, version="1.0.0", **common)
    rr2 = execute_rollout(report=report, version="1.0.0", **common)
    assert rr1.final_state == rr2.final_state == rr2.final_state
    assert verify_external_state(tmp_path / "env", targets=TARGETS, expected_version="1.0.0")


def test_retry_after_rollback_rebuilds_and_can_succeed(tmp_path: Path):
    _deploy_baseline(tmp_path)
    bad = _specs(tmp_path, "1.0.0", health={"canary": "fail"},
                 cache_name="c-bad", work_name="w-bad")
    rr_bad = execute_rollout(report=bad, version="1.0.0",
                             state_dir=tmp_path / "state-new", env_root=tmp_path / "env")
    assert rr_bad.rolled_back
    # 修复后用全新状态重试，应成功
    good = _specs(tmp_path, "1.0.0", cache_name="c-good", work_name="w-good")
    rr_good = execute_rollout(report=good, version="1.0.0",
                              state_dir=tmp_path / "state-fix", env_root=tmp_path / "env")
    assert rr_good.final_state.value == "deployed"
    assert verify_external_state(tmp_path / "env", targets=TARGETS, expected_version="1.0.0")


def test_state_file_allows_upgrade_after_terminal_state(tmp_path: Path):
    """上一版本已部署完成（终态）时，允许在同一环境升级到新版本。"""
    report1 = _specs(tmp_path, "1.0.0", cache_name="c1", work_name="w1")
    execute_rollout(report=report1, version="1.0.0",
                    state_dir=tmp_path / "state", env_root=tmp_path / "env")
    report2 = _specs(tmp_path, "2.0.0", cache_name="c2", work_name="w2")
    rr = execute_rollout(report=report2, version="2.0.0",
                         state_dir=tmp_path / "state", env_root=tmp_path / "env")
    assert rr.final_state.value == "deployed"
    assert verify_external_state(tmp_path / "env", targets=TARGETS, expected_version="2.0.0")


def test_state_file_refuses_version_switch_mid_release(tmp_path: Path):
    """上一轮仍在进行中（无终态标记）就换版本：必须拒绝，防止交错半状态。"""
    from release_pipeline.locking import write_json_atomic

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    write_json_atomic(state_dir / "rollout-state.json", {
        "version": "1.0.0",
        "before": {t: None for t in TARGETS},
        "stages": {"canary": "done"},  # 只推进到 canary，未走完也未回滚
        "final_state": None,
    })
    report = _specs(tmp_path, "2.0.0")
    with pytest.raises(RolloutError) as exc:
        execute_rollout(report=report, version="2.0.0",
                        state_dir=state_dir, env_root=tmp_path / "env")
    assert "拒绝混用发布状态" in exc.value.reason


def test_partial_stage_failure_keeps_unpromoted_targets_on_baseline(tmp_path: Path):
    _deploy_baseline(tmp_path)
    report = _specs(tmp_path, "1.0.0", health={"partial-b": "fail"},
                    cache_name="c-new", work_name="w-new")
    rr = execute_rollout(report=report, version="1.0.0",
                         state_dir=tmp_path / "state-new", env_root=tmp_path / "env")
    assert rr.reached_stage is ReleaseStage.PARTIAL
    assert rr.rolled_back
    # canary 曾被推进到新版本，也必须回到基线
    assert open_target(tmp_path / "env", "canary").current_version == "0.9.0"
    assert open_target(tmp_path / "env", "full").current_version == "0.9.0"


def test_missing_successful_artifact_target_rejects_release(tmp_path: Path):
    specs = [BuildSpec(target="canary", command="sim:fail:build")]
    report = run_matrix(specs, version="1.0.0", repo_root=Path("."),
                        work_root=tmp_path / "work", cache=None,
                        env_digest="e", sources_digest="s")
    assert report.conclusion is MatrixConclusion.FAILURE
    with pytest.raises(RolloutError) as exc:
        execute_rollout(report=report, version="1.0.0",
                        state_dir=tmp_path / "state", env_root=tmp_path / "env",
                        stages=[ReleaseStage.CANARY])
    assert "未成功产出" in exc.value.reason
