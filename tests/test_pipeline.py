"""End-to-end pipeline: repeatability, exit codes, parity, retries."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from release_pipeline.errors import ParityError
from release_pipeline.pipeline import (
    EXIT_BUILD_FAILED,
    EXIT_OK,
    EXIT_ROLLOUT_BLOCKED,
    PipelineConfig,
    run_pipeline,
)

pytestmark = pytest.mark.logging_inputs


def _cfg(work, repo=".", **over):
    return PipelineConfig(repo_dir=over.pop("repo_dir", repo),
                          work_dir=str(work), mode="local", **over)

def test_happy_path_exit_zero_and_idempotent_rerun(tmp_path, git_repo):
    cfg = _cfg(tmp_path / "w", str(git_repo))
    r1 = run_pipeline(cfg)
    r2 = run_pipeline(_cfg(tmp_path / "w", str(git_repo)))
    print(f"\n[test input] repo clean tagged -> {r1.version.version} "
          f"exit1={r1.exit_code} exit2={r2.exit_code}")
    assert r1.exit_code == EXIT_OK
    assert r2.exit_code == EXIT_OK
    assert r1.version == r2.version
    assert {x.reason for x in r2.build.results} == {"cache-hit"}
    assert all(d.reason == "already-fully-rolled-out" for d in r2.deploys)
    # Every target is fully live in its own environment.
    env_root = tmp_path / "w" / "environments"
    for link in env_root.glob("*/current"):
        assert Path(link).resolve().name == r1.version.version


def test_partial_build_failure_exit_2_but_others_live(tmp_path, git_repo):
    cfg = _cfg(
        tmp_path / "w", repo=str(git_repo),
        targets=("t1", "t2"), failing_targets=frozenset({"t2"}),
    )
    r = run_pipeline(cfg)
    failed = [x.target for x in r.build.failed]
    succeeded = [x.target for x in r.build.succeeded]
    print(f"\n[test input] failing={{t2}} -> failed={failed} succeeded={succeeded} "
          f"exit={r.exit_code}")
    assert r.exit_code == EXIT_BUILD_FAILED
    assert failed == ["t2"] and succeeded == ["t1"]
    # t1 artifact deployed through all stages; t2 has neither artifact nor env.
    assert (tmp_path / "w" / "environments" / "t1" / "current").exists()
    assert not (tmp_path / "w" / "environments" / "t2").exists()
    assert not [d for d in r.deploys if d.rolled_back]


def test_health_failure_exit_3_and_rollback(tmp_path, git_repo):
    cfg = _cfg(
        tmp_path / "w", repo=str(git_repo),
        health_policy={"staging": (False, True)},
    )
    r = run_pipeline(cfg)
    print(f"\n[test input] unhealthy staging -> exit={r.exit_code} "
          f"rollbacks={len([d for d in r.deploys if d.rolled_back])}")
    assert r.exit_code == EXIT_ROLLOUT_BLOCKED
    assert all(d.rolled_back for d in r.deploys)
    assert all(d.visible_version is None for d in r.deploys)


def test_inconclusive_health_exit_3(tmp_path, git_repo):
    cfg = _cfg(
        tmp_path / "w", repo=str(git_repo),
        health_policy={"full": (True, False)},
    )
    r = run_pipeline(cfg)
    rolled = [d for d in r.deploys if d.rolled_back]
    print(f"\n[test input] inconclusive full -> exit={r.exit_code} "
          f"reasons={[d.reason for d in rolled]}")
    assert r.exit_code == EXIT_ROLLOUT_BLOCKED
    assert rolled and all("health-inconclusive" in d.reason for d in rolled)


def test_local_and_ci_reach_identical_verdict(tmp_path, git_repo):
    kw = dict(
        repo_dir=str(git_repo),
        env={"PATH": "/usr/bin", "CC": "cc"},
        config={"opt": True},
        health_policy={"staging": (False, True)},
    )
    rl = run_pipeline(PipelineConfig(work_dir=str(tmp_path / "l"),
                                     mode="local", **kw))
    rc = run_pipeline(PipelineConfig(work_dir=str(tmp_path / "c"),
                                     mode="ci", **kw))
    print(f"\n[test input] same explicit inputs local={rl.exit_code} ci={rc.exit_code}")
    assert rl.exit_code == rc.exit_code
    assert rl.version.version == rc.version.version
    assert [b.reason for b in rl.build.results] == \
           [b.reason for b in rc.build.results]
    assert [d.reason for d in rl.deploys] == [d.reason for d in rc.deploys]


def test_ci_rejects_unknown_param_local_degrades(tmp_path, git_repo):
    with pytest.raises(ParityError) as exc:
        run_pipeline(PipelineConfig(
            repo_dir=str(git_repo), work_dir=str(tmp_path / "c"),
            mode="ci", extra_params=frozenset({"mystery"}),
        ))
    assert exc.value.code == "unsupported_param"
    r = run_pipeline(PipelineConfig(
        repo_dir=str(git_repo), work_dir=str(tmp_path / "l"),
        mode="local", extra_params=frozenset({"mystery"}),
    ))
    print(f"\n[test input] unknown param: ci rejected, local degraded={r.degraded}")
    assert "ignored-params" in r.degraded


def test_repeated_runs_share_cache_artifacts_byte_identical(tmp_path, git_repo):
    r1 = run_pipeline(_cfg(tmp_path / "w", str(git_repo)))
    art_dir = tmp_path / "w" / "artifacts"
    first = {p.name: p.read_bytes() for p in art_dir.glob("*.bin")}
    shutil.rmtree(art_dir)
    r2 = run_pipeline(_cfg(tmp_path / "w", str(git_repo)))
    second = {p.name: p.read_bytes() for p in art_dir.glob("*.bin")}
    assert r1.version == r2.version
    assert first == second, "cache-restored artifacts must be byte identical"


def test_crash_leftovers_are_swept_on_retry(tmp_path, git_repo):
    work = tmp_path / "w"
    # Simulate interrupted deploy leaving staging/temp symlinks behind.
    env_root = work / "environments" / "t1" / "releases"
    env_root.mkdir(parents=True)
    (env_root / ".staging-deadbeef").mkdir()
    (work / "environments" / "t1").mkdir(parents=True, exist_ok=True)
    (work / "environments" / "t1" / ".current.tmp-abcd").symlink_to(
        "releases/x")
    r = run_pipeline(_cfg(work, str(git_repo), targets=("t1",)))
    leftovers = list((work / "environments").rglob(".staging-*"))
    leftovers += list((work / "environments").rglob(".current.tmp-*"))
    print(f"\n[test input] stale temp resources present -> exit={r.exit_code}, "
          f"leftovers={len(leftovers)}")
    assert r.exit_code == EXIT_OK
    assert not leftovers


def test_concurrent_pipeline_runs_no_half_state(tmp_path, git_repo):
    # Two processes/runs targeting the same work dir must converge without
    # corrupting state; locking serializes deploy, cache is content-addressed.
    results: list[int] = []
    errors: list[Exception] = []

    def runner():
        try:
            r = run_pipeline(_cfg(tmp_path / "w", str(git_repo),
                                  targets=("t1",)))
            results.append(r.exit_code)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"\n[test input] 3 concurrent same-dir runs -> exits={results} "
          f"errors={errors}")
    assert not errors
    assert results == [EXIT_OK, EXIT_OK, EXIT_OK]
    state = json.loads((tmp_path / "w" / "state" / "t1" / "state.json")
                       .read_text())
    assert state["stage"] == "full"
    assert state["rolled_back"] is False
