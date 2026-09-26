"""Content-addressed build cache.

Cache key coverage
------------------
The key is a SHA-256 over canonical JSON of:

* target name / artifact identity
* resolved version (incl. dirty marker and commit sha)
* full source-tree hash
* environment fingerprint
* build configuration fingerprint
* toolchain identifier
* cache format version (so an incompatible layout never hits)

Any change to one of these inputs yields a different key, so stale entries
can never be served after an environment or configuration change.

Integrity
---------
Every entry stores ``meta.json`` (inputs + content SHA-256) next to the
artifact. Reads re-hash the artifact and re-check every field. Any mismatch,
truncation or unreadable file is treated as *corruption*: the entry is
evicted and the caller rebuilds — a corrupt cache can never produce an
artifact.

Concurrency
-----------
Publishing uses ``temp file -> fsync -> rename`` (atomic on POSIX) inside a
per-key ``fcntl`` lock, so concurrent producers never expose a half-written
entry.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

from .models import Artifact, BuildInput

CACHE_FORMAT_VERSION = 1
_META_NAME = "meta.json"
_ARTIFACT_NAME = "artifact.bin"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_cache_key(inp: BuildInput) -> str:
    """Return the 64-char cache key for a complete build input."""
    payload = {
        "format": CACHE_FORMAT_VERSION,
        "target": inp.target.name,
        "artifact": inp.target.artifact_name,
        "version": inp.version,
        "source_hash": inp.source_hash,
        "env_fingerprint": inp.env_fingerprint,
        "config_fingerprint": inp.config_fingerprint,
        "toolchain": inp.toolchain,
    }
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _sha256_bytes(blob)


class CacheMiss(Exception):
    """Internal: no usable entry (absent or corrupted/rebuilt)."""


def probe_cache_entry(cache_dir: str | Path, inp: BuildInput) -> str:
    """Read-only cache probe: ``"hit" | "miss" | "corrupted"``.

    Unlike :meth:`BuildCache.get`, this never creates the cache directory,
    never takes a lock and **never evicts** a corrupted entry, so dry-run
    planning can predict reuse-vs-rebuild with zero side effects even while a
    real run shares the cache.
    """
    key = compute_cache_key(inp)
    root = Path(cache_dir)
    entry = root / key[:2] / key
    meta_path = entry / _META_NAME
    art_path = entry / _ARTIFACT_NAME
    if not meta_path.is_file() or not art_path.is_file():
        return "miss"
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(art_path, "rb") as fh:
            data = fh.read()
    except (OSError, json.JSONDecodeError):
        return "corrupted"

    expected_fields = {
        "format": CACHE_FORMAT_VERSION,
        "target": inp.target.name,
        "version": inp.version,
        "source_hash": inp.source_hash,
        "env_fingerprint": inp.env_fingerprint,
        "config_fingerprint": inp.config_fingerprint,
        "toolchain": inp.toolchain,
    }
    for field_name, expected in expected_fields.items():
        if meta.get(field_name) != expected:
            return "corrupted"
    if meta.get("content_hash") != _sha256_bytes(data):
        return "corrupted"
    return "hit"


class BuildCache:
    """On-disk cache of artifacts keyed by :func:`compute_cache_key`."""

    def __init__(self, cache_dir: str | Path) -> None:
        self.root = Path(cache_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "locks").mkdir(exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _entry_dir(self, key: str) -> Path:
        # Shard by first 2 chars to keep directory sizes bounded.
        return self.root / key[:2] / key

    def _meta_path(self, key: str) -> Path:
        return self._entry_dir(key) / _META_NAME

    def _artifact_path(self, key: str) -> Path:
        return self._entry_dir(key) / _ARTIFACT_NAME

    def _lock_path(self, key: str) -> Path:
        return self.root / "locks" / key

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _log(msg: str) -> None:
        from .log import log
        log("cache", msg)

    def _evict(self, key: str, reason: str) -> None:
        entry = self._entry_dir(key)
        if entry.exists():
            self._log(f"evict corrupted entry {key[:12]} ({reason})")
            for child in sorted(entry.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            entry.rmdir()
            try:
                entry.parent.rmdir()  # remove empty shard
            except OSError:
                pass

    # ----------------------------------------------------------------- verify
    def verify(self, inp: BuildInput) -> bool:
        """Return True iff a complete, intact entry exists for ``inp``."""
        try:
            self._load(inp)
        except CacheMiss:
            return False
        return True

    def _load(self, inp: BuildInput) -> tuple[Artifact, bytes]:
        key = compute_cache_key(inp)
        meta_path = self._meta_path(key)
        art_path = self._artifact_path(key)
        if not meta_path.is_file() or not art_path.is_file():
            raise CacheMiss("absent")
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(art_path, "rb") as fh:
                data = fh.read()
        except (OSError, json.JSONDecodeError) as exc:
            self._evict(key, f"unreadable: {exc}")
            raise CacheMiss("corrupted") from exc

        # Full re-validation: every field that identifies the input + hash.
        expected_fields = {
            "format": CACHE_FORMAT_VERSION,
            "target": inp.target.name,
            "version": inp.version,
            "source_hash": inp.source_hash,
            "env_fingerprint": inp.env_fingerprint,
            "config_fingerprint": inp.config_fingerprint,
            "toolchain": inp.toolchain,
        }
        for field_name, expected in expected_fields.items():
            if meta.get(field_name) != expected:
                self._evict(
                    key, f"meta mismatch on {field_name}: {meta.get(field_name)!r}"
                )
                raise CacheMiss("corrupted")

        actual_hash = _sha256_bytes(data)
        if meta.get("content_hash") != actual_hash:
            self._evict(
                key,
                f"content hash mismatch: meta={meta.get('content_hash')} "
                f"actual={actual_hash}",
            )
            raise CacheMiss("corrupted")

        artifact = Artifact(
            target=inp.target.name,
            version=inp.version,
            path=str(art_path),
            content_hash=actual_hash,
            cache_hit=True,
            cache_key=key,
        )
        return artifact, data

    # -------------------------------------------------------------------- get
    def get(self, inp: BuildInput) -> Artifact | None:
        """Return a verified artifact, or ``None`` on miss/corruption."""
        try:
            artifact, _ = self._load(inp)
        except CacheMiss:
            return None
        self._log(
            f"HIT target={inp.target.name} key={artifact.cache_key[:12]} "
            f"hash={artifact.content_hash[:12]}"
        )
        return artifact

    # -------------------------------------------------------------------- put
    def put(self, inp: BuildInput, artifact_bytes: bytes) -> Artifact:
        """Publish an artifact atomically, serialized per key with a lock."""
        key = compute_cache_key(inp)
        lock_path = self._lock_path(key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        entry = self._entry_dir(key)
        entry.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            tmp_dir = entry.with_name(entry.name + ".tmp")
            # Leftover from a crashed producer: discard whole staging dir.
            if tmp_dir.exists():
                self._rmtree(tmp_dir)
            tmp_dir.mkdir(parents=True)
            try:
                art_tmp = tmp_dir / _ARTIFACT_NAME
                meta_tmp = tmp_dir / _META_NAME
                content_hash = _sha256_bytes(artifact_bytes)
                with open(art_tmp, "wb") as fh:
                    fh.write(artifact_bytes)
                    fh.flush()
                    os.fsync(fh.fileno())
                meta = {
                    "format": CACHE_FORMAT_VERSION,
                    "target": inp.target.name,
                    "artifact": inp.target.artifact_name,
                    "version": inp.version,
                    "source_hash": inp.source_hash,
                    "env_fingerprint": inp.env_fingerprint,
                    "config_fingerprint": inp.config_fingerprint,
                    "toolchain": inp.toolchain,
                    "content_hash": content_hash,
                }
                with open(meta_tmp, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                # Atomically stage the entry into place.
                final = entry
                final_backup = entry.with_name(entry.name + ".old")
                if final.exists():
                    final.rename(final_backup)
                tmp_dir.rename(final)
                if final_backup.exists():
                    self._rmtree(final_backup)
            except BaseException:
                # Interrupted publish: never leave a .tmp or half entry visible.
                self._rmtree(tmp_dir)
                raise
        self._log(
            f"PUT target={inp.target.name} key={key[:12]} hash={content_hash[:12]}"
        )
        return Artifact(
            target=inp.target.name,
            version=inp.version,
            path=str(self._artifact_path(key)),
            content_hash=content_hash,
            cache_hit=False,
            cache_key=key,
        )

    @staticmethod
    def _rmtree(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            else:
                child.rmdir()
        path.rmdir()
