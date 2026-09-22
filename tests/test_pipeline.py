"""流水线编排：退出码可区分、产物可用范围明确、本地/CI 结论一致、能力门禁。"""

from __future__ import annotations

from pathlib import Path

import pytest

from release_pipeline.models import BuildSpec, MatrixConclusion, ReleaseStage
from release_pipeline.pipeline import (
    EXIT_ALL_FAILED,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_ROLLED_BACK,
    EXIT_ENV_REJECTED,
    EXIT_EMPTY_MATRIX,
    PipelineConfig,
    run,
)
from release_pipeline.runtime import detect_profile

TARGETS = ["canary", "partial-a", "partial-b", "full"]


def _all_ok_specs() -> list[BuildSpec]:
    return [BuildSpec(target=t, command="sim:ok") for t in TARGETS]


def test_full_pipeline_exit_zero_and_reproducible(tmp_path: Path):
    cfg = PipelineConfig(
        repo_root=Path(__file__).resolve().parents[1],
        work_root=tmp_path / "work1",
        specs=_all_ok_specs(),
        explicit_version="4.5.6",
    )
    out1 = run(cfg)
    out2 = run(PipelineConfig(
        repo_root=cfg.repo_root, work_root=tmp_path / "work2",
        specs=_all_ok_specs(), explicit_version="4.5.6",
    ))
    assert out1.exit_code == EXIT_OK == out2.exit_code
    assert out1.version.version == out2.version.version
    # 第二次构建应全部缓存命中（跨工作目录使用各自缓存时 miss，但版本结论一致）
    assert out1.build.conclusion is MatrixConclusion.SUCCESS
    assert out1.rollout and out1.rollout.final_state.value == "deployed"


def test_repeat_run_hits_cache_in_same_workroot(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    cfg = PipelineConfig(repo_root=repo, work_root=tmp_path / "work",
                         specs=_all_ok_specs(), explicit_version="5.0.0")
    run(cfg)
    out = run(cfg)
    cache_states = {r.target: r.cache_status.value for r in out.build.results}
    assert all(v == "hit" for v in cache_states.values()), cache_states


def test_partial_failure_exit_code_and_usable_artifacts(tmp_path: Path):
    specs = [
        BuildSpec(target="canary", command="sim:ok"),
        BuildSpec(target="partial-a", command="sim:fail:build"),
        BuildSpec(target="partial-b", command="sim:ok"),
        BuildSpec(target="full", command="sim:ok"),
    ]
    out = run(PipelineConfig(
        repo_root=Path(__file__).resolve().parents[1],
        work_root=tmp_path / "work", specs=specs, explicit_version="6.0.0",
    ))
    assert out.exit_code == EXIT_PARTIAL_FAILURE, "部分失败必须有独立退出码"
    assert out.build.failed == ("partial-a",)
    assert set(out.build.successful) == {"canary", "partial-b", "full"}
    # 成功目标产物可用；失败目标无产物；且未进入上线
    ok_artifacts = [r.artifact_path for r in out.build.results if r.target in out.build.successful]
    assert all(Path(p).is_file() for p in ok_artifacts)
    assert all(r.artifact_path is None for r in out.build.results if r.target in out.build.failed)
    assert out.rollout is None
    assert "产物可用" in out.summary


def test_all_failure_exit_code(tmp_path: Path):
    specs = [BuildSpec(target="canary", command="sim:fail:build")]
    out = run(PipelineConfig(
        repo_root=Path(__file__).resolve().parents[1],
        work_root=tmp_path / "work", specs=specs, explicit_version="6.0.0",
        stages=[ReleaseStage.CANARY.value] if False else None,
    ))
    assert out.exit_code == EXIT_ALL_FAILED
    assert "无可用产物" in out.summary


def test_empty_matrix_rejected(tmp_path: Path):
    out = run(PipelineConfig(
        repo_root=Path(__file__).resolve().parents[1],
        work_root=tmp_path / "work", specs=[], explicit_version="1.0.0",
    ))
    assert out.exit_code == EXIT_EMPTY_MATRIX


def test_rollback_exit_code_distinct(tmp_path: Path):
    specs = [BuildSpec(target=t, command="sim:ok",
                       options=(("health", "fail" if t == "canary" else "ok"),))
             for t in TARGETS]
    out = run(PipelineConfig(
        repo_root=Path(__file__).resolve().parents[1],
        work_root=tmp_path / "work", specs=specs, explicit_version="7.0.0",
    ))
    assert out.exit_code == EXIT_ROLLED_BACK, "回滚必须有区别于构建失败的退出码"
    assert out.rollout.rolled_back is True


def test_unsupported_stage_argument_rejected(tmp_path: Path):
    out_caller = run
    from release_pipeline.errors import EnvironmentRejected

    with pytest.raises(EnvironmentRejected):
        run(PipelineConfig(
            repo_root=Path(__file__).resolve().parents[1],
            work_root=tmp_path / "work", specs=_all_ok_specs(),
            explicit_version="1.0.0", stages=["full"],  # 跳过 canary，必须拒绝
        ))


def test_local_and_ci_profiles_produce_same_verdict(tmp_path: Path, monkeypatch):
    """profile 只是元数据：本地与注入 CI 环境变量后，版本与构建判定完全一致。"""
    repo = Path(__file__).resolve().parents[1]

    local = detect_profile({})
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    ci = detect_profile()

    assert local.profile == "local"
    assert ci.profile == "ci" and ci.ci_provider == "github-actions"

    specs = _all_ok_specs()
    out_local = run(PipelineConfig(repo_root=repo, work_root=tmp_path / "w-local",
                                   specs=specs, explicit_version="8.0.0", env={}))
    out_ci = run(PipelineConfig(repo_root=repo, work_root=tmp_path / "w-ci",
                                specs=specs, explicit_version="8.0.0",
                                env={"CI": "true", "GITHUB_ACTIONS": "true"}))
    assert out_local.exit_code == out_ci.exit_code == EXIT_OK
    assert out_local.version.version == out_ci.version.version
    local_digests = sorted(r.artifact_digest for r in out_local.build.results)
    ci_digests = sorted(r.artifact_digest for r in out_ci.build.results)
    assert local_digests == ci_digests, "本地与 CI 同输入必须产出同摘要产物"


def test_capability_gate_rejects_missing_capability(tmp_path: Path, monkeypatch):
    from release_pipeline import runtime
    from release_pipeline.models import CapabilityIssue
    from release_pipeline.errors import EnvironmentRejected

    monkeypatch.setattr(
        runtime, "detect_profile",
        lambda *a, **k: detect_profile().__class__(
            profile="local", ci_provider=None, python_version="3",
            git_available=False,
            capabilities=(CapabilityIssue("filesystem-lock", False, "模拟无 fcntl"),),
            normalized={},
        ),
    )
    # pipeline.run 内部导入的是模块内函数引用，需同时 patch pipeline 命名空间
    from release_pipeline import pipeline
    monkeypatch.setattr(pipeline, "detect_profile", runtime.detect_profile)
    with pytest.raises(EnvironmentRejected) as exc:
        run(PipelineConfig(
            repo_root=Path(__file__).resolve().parents[1],
            work_root=tmp_path / "work", specs=_all_ok_specs(),
            explicit_version="1.0.0",
        ))
    assert "filesystem-lock" in str(exc.value)


def test_whitelisted_env_change_busts_cache(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    cfg = PipelineConfig(repo_root=repo, work_root=tmp_path / "work",
                         specs=_all_ok_specs(), explicit_version="9.0.0",
                         env={"RELEASE_TARGET_ARCH": "x64"})
    run(cfg)
    out_changed = run(PipelineConfig(
        repo_root=repo, work_root=tmp_path / "work", specs=_all_ok_specs(),
        explicit_version="9.0.0", env={"RELEASE_TARGET_ARCH": "arm64"},
    ))
    assert all(r.cache_status.value == "miss" for r in out_changed.build.results), (
        "白名单环境变量变化后不得命中旧缓存"
    )
