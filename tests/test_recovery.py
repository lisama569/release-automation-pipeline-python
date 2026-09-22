"""中断恢复：模拟 promote 后、健康门前中断，重试应能继续并得出明确结论。"""

from __future__ import annotations

from pathlib import Path

from release_pipeline.builder import run_matrix
from release_pipeline.cache import BuildCache
from release_pipeline.models import BuildSpec, ReleaseState, ReleaseStage
from release_pipeline.rollout import execute_rollout, open_target
from release_pipeline.locking import read_json

TARGETS = ["canary", "partial-a", "partial-b", "full"]


def _report(tmp_path: Path, version: str):
    specs = [BuildSpec(target=t, command="sim:ok") for t in TARGETS]
    return run_matrix(specs, version=version, repo_root=Path("."),
                      work_root=tmp_path / "work", cache=BuildCache(tmp_path / "cache"),
                      env_digest="e", sources_digest="s")


def test_resume_after_promoted_but_stage_not_marked_done(tmp_path: Path):
    """手工构造中断现场：canary 已 stage+promote，但状态文件阶段还是空。

    重试时 stage/promote 幂等，健康门照常给出结论并把阶段补记为 done。
    """
    version = "3.1.4"
    report = _report(tmp_path, version)
    state_dir = tmp_path / "state"
    env_root = tmp_path / "env"
    state_dir.mkdir(parents=True)

    # 先正常推进 canary 但通过“阶段记录被删”模拟中断
    rr = execute_rollout(report=report, version=version,
                         state_dir=state_dir, env_root=env_root,
                         stages=[ReleaseStage.CANARY])
    assert rr.final_state is ReleaseState.DEPLOYED
    state_file = state_dir / "rollout-state.json"
    state = read_json(state_file)
    state["stages"] = {}
    import json
    (state_file).write_text(json.dumps(state), encoding="utf-8")

    rr2 = execute_rollout(report=report, version=version,
                          state_dir=state_dir, env_root=env_root,
                          stages=[ReleaseStage.CANARY])
    assert rr2.final_state is ReleaseState.DEPLOYED
    assert open_target(env_root, "canary").current_version == version


def test_stage_tmp_leftover_cleaned_on_retry(tmp_path: Path):
    version = "3.1.4"
    report = _report(tmp_path, version)
    env_root = tmp_path / "env"
    target = open_target(env_root, "canary")
    # 伪造一个上次硬中断留下的暂存临时目录
    leftover = target.versions_dir / f".{version}.stage-tmp"
    leftover.mkdir(parents=True)
    (leftover / "payload").write_text("half", encoding="utf-8")

    rr = execute_rollout(report=report, version=version,
                         state_dir=tmp_path / "state", env_root=env_root,
                         stages=[ReleaseStage.CANARY])
    assert rr.final_state is ReleaseState.DEPLOYED
    assert not leftover.exists(), "重试必须清理上次中断遗留的暂存目录"
    assert open_target(env_root, "canary").current_version == version
