"""CLI behavior: exit codes, JSON output, explicit rejection."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.logging_inputs


def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "release_pipeline", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_version_outputs_json(tmp_path, git_repo):
    code, out, err = run_cli("version", "--repo", str(git_repo))
    payload = json.loads(out)
    print(f"\n[test input] version cmd -> {payload['version']} (stderr logs: "
          f"{len(err)} bytes)")
    assert code == 0
    assert payload["version"].startswith("1.0.0+2.g")
    assert payload["dirty"] is False
    assert isinstance(payload["changelog"], list) and len(payload["changelog"]) == 2
    # Diagnostics stay off stdout.
    assert "[pipeline]" not in out and "[cache]" not in out


def test_run_exit_codes(tmp_path, git_repo):
    code, out, _ = run_cli(
        "run", "--repo", str(git_repo), "--work-dir", str(tmp_path / "w"),
        "--targets", "t1,t2", "--fail", "t2",
    )
    payload = json.loads(out)
    print(f"\n[test input] --fail t2 -> exit={code}, summary={payload['summary']}")
    assert code == 2
    assert payload["exit_code"] == 2


def test_health_file_drives_rollback(tmp_path, git_repo):
    health = tmp_path / "h.json"
    health.write_text(json.dumps({"staging": [False, True]}))
    code, out, err = run_cli(
        "run", "--repo", str(git_repo), "--work-dir", str(tmp_path / "w"),
        "--health-file", str(health),
    )
    assert code == 3
    assert json.loads(out)["exit_code"] == 3
    print(f"\n[test input] unhealthy health-file -> exit={code}")


def test_ci_mode_rejects_unknown_set(tmp_path, git_repo):
    code, out, err = run_cli(
        "run", "--repo", str(git_repo), "--mode", "ci",
        "--work-dir", str(tmp_path / "w"), "--set", "bogus=1",
    )
    print(f"\n[test input] ci --set bogus=1 -> exit={code}, err={err.strip()}")
    assert code == 1
    assert "unsupported_param" in err


def test_unknown_argument_rejected(tmp_path, git_repo):
    code, _, err = run_cli("run", "--definitely-not-a-flag")
    assert code == 2
    assert "unrecognized arguments" in err


def test_local_mode_reports_degradation(tmp_path, git_repo):
    code, out, _ = run_cli(
        "run", "--repo", str(git_repo), "--mode", "local",
        "--work-dir", str(tmp_path / "w"), "--set", "bogus=1",
    )
    payload = json.loads(out)
    assert code == 0
    assert "ignored-params" in payload["degraded"]
    print(f"\n[test input] local --set bogus=1 degraded={payload['degraded']}")
