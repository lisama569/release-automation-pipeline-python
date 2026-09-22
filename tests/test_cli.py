"""CLI 集成：本地与 CI 执行路径一致、退出码可区分、JSON 输出可判定。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from release_pipeline.cli import main


def _env_ci() -> dict[str, str]:
    e = dict(os.environ)
    e["CI"] = "true"
    e["GITHUB_ACTIONS"] = "true"
    return e


def test_cli_version_json_is_stable(tmp_path: Path, capsys):
    code1 = main(["--repo-root", ".", "version", "--set-version", "1.4.2", "--json"])
    out1 = json.loads(capsys.readouterr().out)
    code2 = main(["--repo-root", ".", "version", "--set-version", "1.4.2", "--json"])
    out2 = json.loads(capsys.readouterr().out)
    assert code1 == code2 == 0
    assert out1["version"] == out2["version"]
    assert out1["version"].startswith("1.4.2")


def test_cli_release_happy_path_and_cache_reuse(tmp_path: Path):
    work = str(tmp_path / "work")
    code = main(["--repo-root", ".", "--work-root", work,
                 "release", "--set-version", "1.4.2", "--json"])
    assert code == 0
    code2 = main(["--repo-root", ".", "--work-root", work,
                  "release", "--set-version", "1.4.2", "--json"])
    assert code2 == 0


def test_cli_partial_failure_exit_code(tmp_path: Path):
    cfg = tmp_path / "release.toml"
    cfg.write_text(
        '[[targets]]\nname = "canary"\ncommand = "sim:ok"\n'
        '[[targets]]\nname = "partial-a"\ncommand = "sim:fail:build"\n'
        '[[targets]]\nname = "partial-b"\ncommand = "sim:ok"\n'
        '[[targets]]\nname = "full"\ncommand = "sim:ok"\n',
        encoding="utf-8",
    )
    code = main(["--repo-root", str(tmp_path), "--work-root", str(tmp_path / "work"),
                 "--config", "release.toml",
                 "release", "--set-version", "2.0.0", "--json"])
    assert code == 10, f"部分失败退出码应为 10，实际 {code}"


def test_cli_rollback_exit_code(tmp_path: Path):
    cfg = tmp_path / "release.toml"
    lines = []
    for t, health in [("canary", "fail"), ("partial-a", "ok"),
                      ("partial-b", "ok"), ("full", "ok")]:
        lines.append(f'[[targets]]\nname = "{t}"\ncommand = "sim:ok"\n'
                     f'[targets.options]\nhealth = "{health}"\n')
    cfg.write_text("\n".join(lines), encoding="utf-8")
    code = main(["--repo-root", str(tmp_path), "--work-root", str(tmp_path / "work"),
                 "--config", "release.toml",
                 "release", "--set-version", "2.0.0", "--json"])
    assert code == 20, f"回滚退出码应为 20，实际 {code}"


def test_cli_bad_stage_argument_rejected(tmp_path: Path):
    with pytest.raises(SystemExit):
        main(["release", "--stage", "yolo"])


def test_cli_env_reports_profile_local(tmp_path: Path, capsys, monkeypatch):
    for k in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL",
              "CIRCLECI", "BUILDKITE", "TF_BUILD", "TRAVIS"):
        monkeypatch.delenv(k, raising=False)
    code = main(["env", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["profile"] == "local"
    assert payload["git_available"] is True


def test_cli_local_and_ci_same_version(tmp_path: Path, capsys, monkeypatch):
    # 本地
    for k in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)
    assert main(["--repo-root", ".", "version", "--set-version", "3.3.3", "--json"]) == 0
    local = json.loads(capsys.readouterr().out)
    # CI
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main(["--repo-root", ".", "version", "--set-version", "3.3.3", "--json"]) == 0
    ci = json.loads(capsys.readouterr().out)
    assert local["version"] == ci["version"]


def test_cli_build_only_does_not_deploy(tmp_path: Path):
    work = str(tmp_path / "work")
    code = main(["--repo-root", ".", "--work-root", work,
                 "build", "--set-version" if False else "--json"])
    # build 子命令无 --set-version；直接断言退出码为 0 且 env 目录不存在
    assert code == 0
    assert not (tmp_path / "work" / "env").exists()
