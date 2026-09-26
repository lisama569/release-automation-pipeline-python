"""Release records: deterministic, comparable, verifiable run receipts.

Determinism contract
--------------------
A record is derived exclusively from the declared repository root and the
explicit inputs. It contains **no** wall-clock timestamps, **no** absolute
paths, **no** cache-hit/miss reasons and **no** host-dependent fields, so
identical inputs produce byte-identical records across repeated runs, across
working directories and across local/CI modes. Fields that legitimately vary
between identical reruns (e.g. ``cache_hit``, ``reason`` strings, deploy
attempt counters) are deliberately excluded.

Verifiability
-------------
:func:`verify_record` re-hashes the artifacts the record refers to and
reports every disagreement; a record that does not match reality is always
discoverable, never silently accepted.

Comparability
-------------
:func:`diff_records` walks two records and returns a machine-readable list
of changed paths, classified by section (``inputs`` vs results), so callers
can tell *which input or which outcome* changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from .models import BuildReport, VersionInfo

RECORD_FORMAT_VERSION = 1


def build_record(
    *,
    version: VersionInfo,
    targets: tuple[str, ...],
    stages: tuple[str, ...],
    failing_targets: frozenset[str],
    health_policy: dict[str, tuple[bool, bool]],
    env_fingerprint: str,
    config_fingerprint: str,
    source_hash: str,
    build: BuildReport,
    final_states: tuple[dict, ...],
    exit_code: int,
    failure_kind: str,
    deploy_blocked: bool,
) -> dict:
    """Assemble the canonical record for a finished run.

    ``final_states`` carries one entry per requested target:
    ``{"target", "final_stage", "rolled_back", "visible_version"}``.
    """
    target_entries = []
    for result in build.results:
        artifact = None
        if result.artifact is not None:
            artifact = {
                # Logical identity + integrity digest only; never a path.
                "name": Path(result.artifact.path).name,
                "content_hash": result.artifact.content_hash,
                "cache_key": result.artifact.cache_key,
            }
        target_entries.append(
            {
                "name": result.target,
                "status": result.status.value,
                "artifact": artifact,
            }
        )

    return {
        "format": RECORD_FORMAT_VERSION,
        "version": {
            "version": version.version,
            "commit": version.commit,
            "dirty": version.dirty,
            "commit_count": version.commit_count,
            "source": version.source,
            "reproducible_hash": version.reproducible_hash,
            "changelog": [[sha, subject] for sha, subject in version.changelog],
        },
        "inputs": {
            "targets": list(targets),
            "stages": list(stages),
            "failing_targets": sorted(failing_targets),
            "health_policy": {
                k: [bool(v[0]), bool(v[1])]
                for k, v in sorted(health_policy.items())
            },
            "env_fingerprint": env_fingerprint,
            "config_fingerprint": config_fingerprint,
            "source_hash": source_hash,
        },
        "targets": target_entries,
        "stages_final": [dict(s) for s in final_states],
        "outcome": {
            "exit_code": exit_code,
            "failure_kind": failure_kind,
            "deploy_blocked": deploy_blocked,
        },
    }


def canonical_record_bytes(record: dict) -> bytes:
    """Serialize a record deterministically (byte-stable for equal input)."""
    blob = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return blob + b"\n"


def write_record(record: dict, records_dir: str | Path) -> str:
    """Persist a record atomically; return its path.

    Same inputs => same version => same file name => the file is overwritten
    with byte-identical content, so repeated runs converge instead of
    accumulating duplicates.
    """
    out_dir = Path(records_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"record-{record['version']['version']}.json"
    tmp = final.with_name(final.name + f".tmp-{uuid.uuid4().hex[:8]}")
    data = canonical_record_bytes(record)
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return str(final)


def _walk(value: object, path: str, changes: list[dict], other: object) -> None:
    """Recursive structural compare; appends change entries to ``changes``."""
    if isinstance(value, dict) and isinstance(other, dict):
        for key in sorted(set(value) | set(other)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in value:
                changes.append({"path": sub, "before": None, "after": other[key]})
            elif key not in other:
                changes.append({"path": sub, "before": value[key], "after": None})
            else:
                _walk(value[key], sub, changes, other[key])
    elif isinstance(value, list) and isinstance(other, list):
        if len(value) != len(other):
            changes.append({"path": path, "before": value, "after": other})
            return
        for i, (a, b) in enumerate(zip(value, other)):
            _walk(a, f"{path}[{i}]", changes, b)
    elif value != other:
        changes.append({"path": path, "before": value, "after": other})


def diff_records(before: dict, after: dict) -> dict:
    """Machine-readable diff of two records.

    Returns ``{"identical": bool, "changes": [...]}`` where each change has
    a dotted ``path``, the top-level ``section`` (e.g. ``inputs`` vs
    ``targets`` vs ``outcome``) and the ``before``/``after`` values.
    """
    changes: list[dict] = []
    _walk(before, "", changes, after)
    for change in changes:
        path = change["path"]
        change["section"] = path.split(".", 1)[0].split("[", 1)[0] or "root"
    changes.sort(key=lambda c: c["path"])
    return {"identical": not changes, "changes": changes}


def verify_record(record: dict, work_dir: str | Path) -> dict:
    """Check the record against the artifacts actually on disk.

    Returns ``{"ok": bool, "mismatches": [...]}``; every referenced artifact
    must exist under ``<work_dir>/artifacts/`` and its SHA-256 must equal the
    recorded ``content_hash``. Any disagreement is reported, never ignored.
    """
    artifacts_dir = Path(work_dir) / "artifacts"
    mismatches: list[dict] = []
    for entry in record.get("targets", []):
        artifact = entry.get("artifact")
        if artifact is None:
            continue
        path = artifacts_dir / artifact["name"]
        if not path.is_file():
            mismatches.append(
                {
                    "target": entry.get("name"),
                    "field": "artifact",
                    "problem": "missing",
                    "recorded": artifact["name"],
                    "actual": None,
                }
            )
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != artifact["content_hash"]:
            mismatches.append(
                {
                    "target": entry.get("name"),
                    "field": "content_hash",
                    "problem": "digest_mismatch",
                    "recorded": artifact["content_hash"],
                    "actual": actual,
                }
            )
    return {"ok": not mismatches, "mismatches": mismatches}
