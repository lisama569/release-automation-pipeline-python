"""Command line interface.

Examples
--------
    python -m release_pipeline version --repo .
    python -m release_pipeline build --repo . --work-dir .w \
        --target t1,t2 --fail t2
    python -m release_pipeline run --repo . --mode ci --env-file env.json
    python -m release_pipeline verify-parity --mode ci

Unknown options are never silently swallowed: argparse rejects them, and the
``--set`` key/value pairs are checked against the supported parameter set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .builder import run_matrix
from .cache import BuildCache
from .errors import PipelineError
from .models import BuildTarget
from .parity import SUPPORTED_PARAMS, check_parity
from .pipeline import PipelineConfig, run_pipeline
from .plan import plan_pipeline
from .record import diff_records, verify_record
from .versioning import derive_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_pipeline",
        description="Local-first reproducible release automation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=".", help="repository root")
        p.add_argument("--work-dir", default=".release-work")
        p.add_argument("--mode", choices=("local", "ci"), default="local")
        p.add_argument(
            "--declared-version",
            default=None,
            help="MAJOR.MINOR.PATCH; must agree with any git tag base",
        )

    pv = sub.add_parser("version", help="derive and print the version")
    add_common(pv)

    pb = sub.add_parser("build", help="run the build matrix only")
    add_common(pb)
    pb.add_argument("--targets", default="linux-x86_64,linux-aarch64")
    pb.add_argument("--fail", default="", help="comma list of failing targets")
    pb.add_argument("--env-file", default=None)
    pb.add_argument("--config-file", default=None)

    pr = sub.add_parser("run", help="full pipeline incl. staged rollout")
    add_common(pr)
    pr.add_argument("--targets", default="linux-x86_64,linux-aarch64")
    pr.add_argument("--fail", default="")
    pr.add_argument("--env-file", default=None)
    pr.add_argument("--config-file", default=None)
    pr.add_argument("--stages", default="staging,canary,full")
    pr.add_argument(
        "--health-file",
        default=None,
        help='JSON map stage -> [healthy, conclusive], e.g. '
        '{"staging": [false, true]}',
    )
    pr.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extra pipeline parameters (validated; rejected in ci mode)",
    )

    pd = sub.add_parser(
        "plan",
        help="dry-run: predict reuse/rebuild and stage progression "
        "without touching anything",
    )
    add_common(pd)
    pd.add_argument("--targets", default="linux-x86_64,linux-aarch64")
    pd.add_argument("--fail", default="")
    pd.add_argument("--env-file", default=None)
    pd.add_argument("--config-file", default=None)
    pd.add_argument("--stages", default="staging,canary,full")
    pd.add_argument("--health-file", default=None)
    pd.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        metavar="KEY=VALUE",
    )

    pvrec = sub.add_parser(
        "verify-record",
        help="check a release record against the artifacts on disk",
    )
    pvrec.add_argument("--work-dir", default=".release-work")
    pvrec.add_argument(
        "--record",
        default=None,
        help="record file; defaults to the newest in <work-dir>/records",
    )

    pdiff = sub.add_parser(
        "diff-records",
        help="machine-readable diff of two release records",
    )
    pdiff.add_argument("before")
    pdiff.add_argument("after")

    pp = sub.add_parser("verify-parity", help="check local/CI parity contract")
    pp.add_argument("--mode", choices=("local", "ci"), default="local")
    pp.add_argument("--env-file", default=None)
    pp.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        metavar="KEY=VALUE",
    )
    return parser


def _load_json_file(path: str | None) -> dict | None:
    if path is None:
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise PipelineError(f"{path} must contain a JSON object",
                            code="bad_config_file")
    return data


def _extra_params(sets: list[str]) -> frozenset[str]:
    keys: set[str] = set()
    for item in sets:
        if "=" not in item:
            raise PipelineError(
                f"--set expects KEY=VALUE, got {item!r}",
                code="bad_set_argument",
            )
        keys.add(item.split("=", 1)[0])
    return frozenset(keys)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            info = derive_version(args.repo, declared=args.declared_version)
            print(json.dumps(
                {
                    "version": info.version,
                    "commit": info.commit,
                    "dirty": info.dirty,
                    "commit_count": info.commit_count,
                    "source": info.source,
                    "reproducible_hash": info.reproducible_hash,
                    "changelog": [
                        {"sha": s, "subject": sub} for s, sub in info.changelog
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ))
            return 0

        if args.command == "verify-parity":
            env = _load_json_file(args.env_file)
            report = check_parity(
                mode=args.mode, env=env,
                given_params=_extra_params(args.sets),
            )
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0

        if args.command == "verify-record":
            record_path = args.record
            if record_path is None:
                candidates = sorted(
                    Path(args.work_dir).glob("records/record-*.json")
                )
                if not candidates:
                    raise PipelineError(
                        f"no records found under {args.work_dir}/records",
                        code="record_missing",
                    )
                record_path = str(candidates[-1])
            with open(record_path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
            report = verify_record(record, args.work_dir)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1

        if args.command == "diff-records":
            with open(args.before, "r", encoding="utf-8") as fh:
                before = json.load(fh)
            with open(args.after, "r", encoding="utf-8") as fh:
                after = json.load(fh)
            report = diff_records(before, after)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["identical"] else 1

        env = _load_json_file(getattr(args, "env_file", None))
        config = _load_json_file(getattr(args, "config_file", None))
        health = _load_json_file(getattr(args, "health_file", None))
        health_policy = (
            {k: tuple(v) for k, v in health.items()} if health else {}
        )
        extra = _extra_params(getattr(args, "sets", []))
        # Early, explicit validation: unknown params are not silently ignored.
        unknown = sorted(extra - SUPPORTED_PARAMS)
        if unknown and args.mode == "ci":
            raise PipelineError(
                f"unsupported parameters rejected in ci mode: {unknown}",
                code="unsupported_param",
            )

        if args.command == "build":
            # Honor parity even for the build-only path.
            check_parity(mode=args.mode, env=env, given_params=extra)
            info = derive_version(args.repo, declared=args.declared_version)
            targets = [
                BuildTarget(
                    name=n,
                    artifact_name=f"app-{i}-{n}.bin",
                    fail=n in _split_csv(args.fail),
                )
                for i, n in enumerate(_split_csv(args.targets))
            ]
            cache = BuildCache(Path(args.work_dir) / "cache")
            report = run_matrix(
                targets,
                info,
                cache,
                str(Path(args.work_dir) / "artifacts"),
                env_fingerprint=None,
            )
            print(
                json.dumps(
                    {
                        "version": info.version,
                        "succeeded": [r.target for r in report.succeeded],
                        "failed": [
                            {"target": r.target, "reason": r.reason}
                            for r in report.failed
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if report.all_succeeded else 2

        if args.command in ("run", "plan"):
            cfg = PipelineConfig(
                repo_dir=args.repo,
                work_dir=args.work_dir,
                targets=_split_csv(args.targets),
                failing_targets=frozenset(_split_csv(args.fail)),
                declared_version=args.declared_version,
                env=env,
                config=config,
                mode=args.mode,
                stages=_split_csv(args.stages),
                health_policy=health_policy,
                extra_params=extra,
            )
            if args.command == "plan":
                plan = plan_pipeline(cfg)
                print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
                return 0
            result = run_pipeline(cfg)
            print(json.dumps(
                {
                    "exit_code": result.exit_code,
                    "failure_kind": result.failure_kind,
                    "summary": result.summary,
                    "degraded": result.degraded,
                    "record": result.record,
                    "deploy": [
                        {
                            "version": d.version,
                            "stage": d.stage.value,
                            "rolled_back": d.rolled_back,
                            "visible": d.visible_version,
                            "reason": d.reason,
                        }
                        for d in result.deploys
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ))
            return result.exit_code

        parser.error(f"unknown command {args.command!r}")
        return 2
    except PipelineError as exc:
        print(f"error[{exc.code}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
