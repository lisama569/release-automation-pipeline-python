"""Deterministic, comparable release records.

A record is produced after every real execution. It contains no wall-clock
fields, no absolute paths and no execution-mode fields, so the same request
executed repeatedly — locally or in CI — yields byte-identical records.

Record layout (format 1)
-----------------------
::

    {
      "format": 1,
      "id": "<sha256 over the canonical record body>",
      "version": {...},                 # version string, commit, changelog...
      "inputs": {...},                  # fingerprints of explicit inputs
      "targets": [
        {
          "target": "...",
          "build_status": "success|failed",
          "build_reason": "cache-hit|built|build-error: ...",
          "artifact_name": "...",      # deterministic, index-based name
          "cache_key": "...",
          "content_hash": "...",       # "" for failed targets
          "final_stage": "staging|canary|full|",
          "rollout_blocked": false
        }, ...
      ],
      "outcome": "all-succeeded|partial-failure|all-failed[+rollout-blocked]",
      "exit_code": 0
    }

The ``built`` vs ``cache-hit`` distinction is intentionally *not* in the
record: it is a property of the machine's prior cache state, not of the
request or its result. The stable build reason is recorded separately as
``build_reason`` only for failures.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import PipelineError
from .models import TargetStatus

RECORD_FORMAT = 1
RECORDS_DIRNAME = "records"
_CACHE_DIRNAME = "cache"
_ARTIFACTS_DIRNAME = "artifacts"


@dataclass(frozen=True)
class RecordDiff:
    """Structured difference between two records."""

    equal: bool
    changes: tuple[str, ...]


class RecordError(PipelineError):
    """A record is unreadable, malformed or fails self-consistency."""

    code = "record_error"


def _canonical_dumps(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def build_record(result: object) -> dict:
    """Build the canonical record dict from a :class:`PipelineResult`."""
    targets = []
    for i, tr in enumerate(result.build.results):
        artifact_name = ""
        cache_key = ""
        content_hash = ""
        if tr.artifact is not None:
            # Keep only the basename: deterministic across work dirs.
            artifact_name = Path(tr.artifact.path).name
            cache_key = tr.artifact.cache_key
            content_hash = tr.artifact.content_hash
        if tr.status is TargetStatus.FAILED:
            build_reason = tr.reason
        else:
            # cache-hit vs built is machine state, not request state.
            build_reason = "success"
        outcome = next(
            (o for o in result.target_outcomes if o.target == tr.target), None
        )
        targets.append(
            {
                "target": tr.target,
                "build_status": tr.status.value,
                "build_reason": build_reason,
                "artifact_name": artifact_name,
                "cache_key": cache_key,
                "content_hash": content_hash,
                "final_stage": outcome.final_stage if outcome else "",
                "rollout_blocked": bool(
                    outcome.rollout_blocked if outcome else False
                ),
            }
        )
    version = result.version
    body = {
        "format": RECORD_FORMAT,
        "version": {
            "version": version.version,
            "commit": version.commit,
            "dirty": version.dirty,
            "commit_count": version.commit_count,
            "source": version.source,
            "reproducible_hash": version.reproducible_hash,
            "changelog": [[s, sub] for s, sub in version.changelog],
        },
        "inputs": dict(result.request_inputs),
        "targets": targets,
        "outcome": result.outcome,
        "exit_code": result.exit_code,
    }
    body["id"] = record_id(body)
    return body


def record_id(record: dict) -> str:
    """Content hash over the record body (everything but the ``id``)."""
    basis = {k: v for k, v in record.items() if k != "id"}
    return hashlib.sha256(_canonical_dumps(basis).encode("utf-8")).hexdigest()


def record_to_bytes(record: dict) -> bytes:
    """Serialize a record deterministically (canonical JSON, trailing NL)."""
    # Stable, human-readable but byte-exact: sorted keys, 2-space indent.
    text = json.dumps(
        record, sort_keys=True, indent=2, ensure_ascii=False
    )
    return (text + "\n").encode("utf-8")


def write_record(record: dict, records_dir: str | Path) -> Path:
    """Atomically persist a record; returns its path. Execution-only."""
    root = Path(records_dir)
    root.mkdir(parents=True, exist_ok=True)
    # Filename is the content id: identical records never duplicate.
    final = root / f"{record['id']}.json"
    if final.is_file():
        return final
    # Unique temp name per writer: concurrent executions with identical
    # records converge on the same final file without racing on one tmp.
    tmp = final.with_name(f".{record['id']}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(record_to_bytes(record))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return final


def load_record(path: str | Path) -> dict:
    """Load a record from disk."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordError(
            f"cannot read record {path}: {exc}", code="record_unreadable"
        ) from exc
    if not isinstance(record, dict) or record.get("format") != RECORD_FORMAT:
        raise RecordError(
            f"record {path} is not a format-{RECORD_FORMAT} JSON object",
            code="record_bad_format",
        )
    if record.get("id") != record_id(record):
        raise RecordError(
            f"record {path} id does not match its content",
            code="record_id_mismatch",
        )
    return record


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_record(record: dict, work_dir: str | Path) -> tuple[str, ...]:
    """Check the record against actual artifacts; return mismatch codes.

    Empty tuple means the record is fully self-consistent with what is on
    disk. Possible codes:

    * ``record_id_mismatch`` — record body changed but id was not recomputed
    * ``artifact_missing:<target>`` — referenced artifact not on disk
    * ``artifact_hash_mismatch:<target>`` — on-disk bytes disagree with record
    * ``cache_missing:<target>`` — cache entry for cache_key is absent
    * ``cache_hash_mismatch:<target>`` — cached bytes disagree with record
    * ``cache_meta_mismatch:<target>`` — cache metadata disagrees with record
    """
    mismatches: list[str] = []
    if record.get("id") != record_id(record):
        mismatches.append("record_id_mismatch")

    root = Path(work_dir)
    artifacts_dir = root / _ARTIFACTS_DIRNAME
    cache_dir = root / _CACHE_DIRNAME
    for tr in record.get("targets", []):
        name = tr.get("target", "?")
        if tr.get("build_status") != "success":
            continue
        art_path = artifacts_dir / tr["artifact_name"]
        if not art_path.is_file():
            mismatches.append(f"artifact_missing:{name}")
        elif _sha256_file(art_path) != tr["content_hash"]:
            mismatches.append(f"artifact_hash_mismatch:{name}")
        entry = cache_dir / tr["cache_key"][:2] / tr["cache_key"]
        cache_art = entry / "artifact.bin"
        meta_path = entry / "meta.json"
        if not cache_art.is_file():
            mismatches.append(f"cache_missing:{name}")
        elif _sha256_file(cache_art) != tr["content_hash"]:
            mismatches.append(f"cache_hash_mismatch:{name}")
        else:
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, json.JSONDecodeError):
                mismatches.append(f"cache_meta_mismatch:{name}")
            else:
                if meta.get("content_hash") != tr["content_hash"] \
                        or meta.get("target") != name:
                    mismatches.append(f"cache_meta_mismatch:{name}")
    return tuple(mismatches)


def _path_values(label: str, a: object, b: object) -> tuple[str, ...]:
    """Emit one change label per differing JSON leaf (deterministic order)."""
    changes: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                changes.append(f"{label}.{key}: <absent> -> {b[key]!r}")
            elif key not in b:
                changes.append(f"{label}.{key}: {a[key]!r} -> <absent>")
            else:
                changes.extend(_path_values(f"{label}.{key}", a[key], b[key]))
        return tuple(changes)
    if isinstance(a, list) and isinstance(b, list):
        if a == b:
            return ()
        # Lists of records: align on their identity field when present.
        return (f"{label}: {json.dumps(a, sort_keys=True)!r} -> "
                f"{json.dumps(b, sort_keys=True)!r}",)
    if a != b:
        changes.append(f"{label}: {a!r} -> {b!r}")
    return tuple(changes)


def compare_records(a: dict, b: dict) -> "RecordDiff":
    """Point out exactly which inputs/results changed between two records.

    Targets are aligned by target name, so a changed hash or stage is
    reported per-target instead of as an opaque blob difference.
    """
    changes: list[str] = []
    if a.get("id") == b.get("id"):
        return RecordDiff(equal=True, changes=())

    # Scalar sections: deep, path-prefixed comparison.
    for section in ("version", "inputs", "outcome", "exit_code", "format"):
        changes.extend(
            _path_values(section, a.get(section), b.get(section))
        )

    def _by_target(rec: dict) -> dict:
        return {t["target"]: t for t in rec.get("targets", [])}

    ta, tb = _by_target(a), _by_target(b)
    for name in sorted(set(ta) | set(tb)):
        if name not in ta:
            changes.append(f"targets.{name}: <absent> -> added")
            continue
        if name not in tb:
            changes.append(f"targets.{name}: removed -> <absent>")
            continue
        for field_name in (
            "build_status",
            "build_reason",
            "artifact_name",
            "cache_key",
            "content_hash",
            "final_stage",
            "rollout_blocked",
        ):
            va, vb = ta[name].get(field_name), tb[name].get(field_name)
            if va != vb:
                changes.append(
                    f"targets.{name}.{field_name}: {va!r} -> {vb!r}"
                )
    return RecordDiff(equal=not changes, changes=tuple(changes))
