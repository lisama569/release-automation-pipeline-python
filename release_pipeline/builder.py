"""Multi-target build matrix.

Isolation guarantees
--------------------
* Every target builds independently in its own scratch directory; a failure
  in target A is captured as that target's :class:`TargetResult` and never
  changes target B's status.
* The builder never raises for build failures — callers distinguish
  per-target outcomes through :class:`BuildReport`.
* Artifacts are produced through the content-addressed cache. The cache key
  includes the full source-tree hash, so the artifact is only reused when
  every relevant input is identical.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .cache import BuildCache
from .log import log
from .models import (
    Artifact,
    BuildInput,
    BuildReport,
    BuildTarget,
    TargetResult,
    TargetStatus,
    VersionInfo,
)

# Only these environment variables may influence artifacts; an explicit
# override replaces the ambient set (CI parity).
DEFAULT_ENV_VARS: tuple[str, ...] = (
    "PATH",
    "PYTHONPATH",
    "CC",
    "CXX",
    "CFLAGS",
    "LDFLAGS",
    "TARGET_ARCH",
    "BUILD_PROFILE",
)


def fingerprint_environment(overrides: dict[str, str] | None = None) -> str:
    """Deterministic fingerprint of build-affecting environment variables.

    With ``overrides`` the fingerprint is computed solely from the supplied
    mapping (sorted), so a CI run can reproduce a local run exactly.
    """
    if overrides is not None:
        source = overrides
    else:
        source = {k: os.environ.get(k, "") for k in DEFAULT_ENV_VARS}
    blob = json.dumps(
        source, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def fingerprint_config(config: dict[str, object]) -> str:
    """Deterministic fingerprint of the resolved build configuration."""
    blob = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def hash_source_tree(root: str) -> str:
    """Hash all tracked-looking files under ``root`` deterministically.

    ``.git``, caches and work dirs are excluded. Files are hashed as
    ``relative_path + NUL + sha256(content)`` records sorted by path, so
    path/content changes are the only things that change the hash.
    """
    excluded_dirs = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".release-work",
        ".venv",
        "dist",
        "build",
    }
    h = hashlib.sha256()
    root_path = Path(root)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded_dirs)
        for name in sorted(filenames):
            files.append(Path(dirpath) / name)
    for fpath in sorted(files):
        rel = fpath.relative_to(root_path).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(hashlib.sha256(fpath.read_bytes()).digest())
        h.update(b"\x00")
    return h.hexdigest()


def _produce_artifact_bytes(target: BuildTarget, version: VersionInfo) -> bytes:
    """Deterministic simulated compiler output.

    The payload is fully determined by target + version (+ source via cache
    key); randomness is never used, so identical inputs produce byte-identical
    artifacts. A target flagged ``fail`` raises after its scratch work.
    """
    # The build "compilation" is a canonical serialization of its inputs.
    payload = json.dumps(
        {
            "target": target.name,
            "artifact": target.artifact_name,
            "version": version.version,
            "commit": version.commit,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload


def planned_content_hash(target: BuildTarget, version: VersionInfo) -> str:
    """SHA-256 of the artifact a build would produce, without building.

    Pure function of the same inputs as :func:`_produce_artifact_bytes`, so
    a dry-run can state exactly which content hash a rebuild would publish.
    """
    return hashlib.sha256(_produce_artifact_bytes(target, version)).hexdigest()


def build_one(
    target: BuildTarget,
    version: VersionInfo,
    cache: BuildCache,
    artifacts_dir: str,
    *,
    env_fingerprint: str,
    config_fingerprint: str,
    toolchain: str = "python3",
    source_hash: str | None = None,
) -> TargetResult:
    """Build (or restore from cache) a single target.

    Never raises for build failures: returns a FAILED result with a reason
    containing a distinct code. Scratch directories are always cleaned up.
    """
    src_hash = source_hash or hash_source_tree(".")
    inp = BuildInput(
        target=target,
        version=version.version,
        source_hash=src_hash,
        env_fingerprint=env_fingerprint,
        config_fingerprint=config_fingerprint,
        toolchain=toolchain,
    )
    log(
        "builder",
        "build target",
        target=target.name,
        version=version.version,
        src=src_hash[:12],
        env=env_fingerprint[:12],
        cfg=config_fingerprint[:12],
    )

    cached = cache.get(inp)
    if cached is not None:
        published = _publish_artifact(cached, target, artifacts_dir)
        return TargetResult(
            target=target.name,
            status=TargetStatus.SUCCESS,
            artifact=published,
            reason="cache-hit",
        )

    scratch = Path(artifacts_dir) / f".scratch-{target.name}-{uuid.uuid4().hex[:8]}"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        data = _produce_artifact_bytes(target, version)
        if target.fail:
            # Simulated compiler failure: scratch exists but is discarded.
            log("builder", f"target={target.name} FAILED (simulated)")
            return TargetResult(
                target=target.name,
                status=TargetStatus.FAILED,
                artifact=None,
                reason="build-error: target configured to fail",
            )
        stored = cache.put(inp, data)
        published = _publish_artifact(stored, target, artifacts_dir)
        return TargetResult(
            target=target.name,
            status=TargetStatus.SUCCESS,
            artifact=published,
            reason="built",
        )
    except OSError as exc:
        log("builder", f"target={target.name} FAILED (io: {exc})")
        return TargetResult(
            target=target.name,
            status=TargetStatus.FAILED,
            artifact=None,
            reason=f"build-error: io failure: {exc}",
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _publish_artifact(
    stored: Artifact, target: BuildTarget, artifacts_dir: str
) -> Artifact:
    """Materialize the cached artifact under artifacts_dir atomically."""
    out_dir = Path(artifacts_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / target.artifact_name
    tmp = final.with_name(final.name + f".tmp-{uuid.uuid4().hex[:8]}")
    data = Path(stored.path).read_bytes()
    # Re-verify at publish time; never copy a torn cache file onward.
    if hashlib.sha256(data).hexdigest() != stored.content_hash:
        raise OSError(f"cached artifact {target.name} failed re-verification")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return Artifact(
        target=target.name,
        version=stored.version,
        path=str(final),
        content_hash=stored.content_hash,
        cache_hit=stored.cache_hit,
        cache_key=stored.cache_key,
    )


def run_matrix(
    targets: list[BuildTarget],
    version: VersionInfo,
    cache: BuildCache,
    artifacts_dir: str,
    *,
    env_fingerprint: str | None = None,
    config_fingerprint: str = "",
    toolchain: str = "python3",
    source_hash: str | None = None,
    max_workers: int | None = None,
) -> BuildReport:
    """Build every target independently; failures stay per-target.

    Targets may run concurrently but each writes to its own artifact name and
    its own cache key, so they cannot cross-contaminate. Results are returned
    in the declared target order regardless of completion order.
    """
    env_fp = env_fingerprint or fingerprint_environment()
    src_hash = source_hash or hash_source_tree(".")
    Path(artifacts_dir).mkdir(parents=True, exist_ok=True)

    results_by_name: dict[str, TargetResult] = {}
    # Duplicate target names would race on one artifact path: reject.
    if len({t.name for t in targets}) != len(targets):
        raise ValueError("duplicate target names in build matrix")

    workers = max_workers or min(8, max(1, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                build_one,
                t,
                version,
                cache,
                artifacts_dir,
                env_fingerprint=env_fp,
                config_fingerprint=config_fingerprint,
                toolchain=toolchain,
                source_hash=src_hash,
            ): t
            for t in targets
        }
        for fut in as_completed(futures):
            res = fut.result()  # build_one never raises for build errors
            results_by_name[res.target] = res

    ordered = tuple(results_by_name[t.name] for t in targets)
    key_basis = hashlib.sha256(
        (src_hash + env_fp + config_fingerprint + toolchain + version.version)
        .encode("utf-8")
    ).hexdigest()[:16]
    report = BuildReport(
        version=version.version, cache_key=key_basis, results=ordered
    )
    n_ok = len(report.succeeded)
    n_bad = len(report.failed)
    log(
        "builder",
        "matrix summary",
        succeeded=n_ok,
        failed=n_bad,
        all_succeeded=report.all_succeeded,
    )
    return report
