"""Persistent release state with advisory locking and atomic writes.

Concurrency / crash safety
--------------------------
* All reads and writes of ``state.json`` happen while holding an exclusive
  ``fcntl`` lock on ``state.lock``; two pipeline processes can never observe
  or persist a half-updated state.
* Persistence is ``temp file -> fsync -> rename``: a crash leaves either the
  previous file or the new one, never a truncated mix.
* The state deliberately contains no wall-clock timestamps: given the same
  operations it is byte-stable, which is what local/CI parity is checked on.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
from typing import Iterator

from .errors import StateError
from .log import log
from .models import RolloutStage

STATE_FILE = "state.json"
LOCK_FILE = "state.lock"


def new_state(version: str) -> dict:
    """Return the canonical initial state for a release of ``version``."""
    return {
        "version": version,
        "stage": RolloutStage.NONE.value,
        "previous_version": None,
        "visible_version": None,
        "deploy_attempts": 0,
        "rolled_back": False,
        "history": [],
    }


def read_state_readonly(state_dir: str | Path) -> dict:
    """Read ``state.json`` without creating anything.

    Used by dry-run planning and record collection, which must not leave
    traces: no directory creation, no lock-file touch. Writers persist via
    atomic rename, so a lock-free read observes either the old or the new
    file, never a torn one. Missing/unreadable state reads as ``{}``.
    """
    path = Path(state_dir) / STATE_FILE
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class ReleaseStateStore:
    """JSON state file guarded by an exclusive advisory lock."""

    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / STATE_FILE
        self.lock_path = self.dir / LOCK_FILE
        # Pre-create the lock file so locking never races on creation.
        self.lock_path.touch(exist_ok=True)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        # Separate file handle per entry so the context is re-entrant-safe
        # across threads/processes at the OS lock level.
        fh = open(self.lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def exists(self) -> bool:
        return self.state_path.is_file()

    def read(self) -> dict:
        """Return the current state, or an empty dict if not initialized."""
        with self._locked():
            if not self.state_path.is_file():
                return {}
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise StateError(
                    f"state file unreadable: {exc}", code="state_corrupted"
                ) from exc
            if not isinstance(data, dict):
                raise StateError("state file is not a JSON object",
                                 code="state_corrupted")
            return data

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict]:
        """Yield mutable state; persist atomically on clean exit.

        If the body raises, nothing is written. A ``reset`` callable is
        attached as ``state['__reset__']``? No — callers simply mutate the
        returned dict; initialization starts from :func:`new_state` when the
        caller seeds it. Missing file => empty dict that the caller seeds.
        """
        with self._locked():
            if self.state_path.is_file():
                try:
                    with open(self.state_path, "r", encoding="utf-8") as fh:
                        state = json.load(fh)
                except json.JSONDecodeError as exc:
                    raise StateError(
                        f"refusing to overwrite unreadable state: {exc}",
                        code="state_corrupted",
                    ) from exc
            else:
                state = {}
            try:
                yield state
            except BaseException:
                # Caller failed mid-update: drop the in-memory copy, keep disk.
                log("state", "transaction aborted; on-disk state unchanged")
                raise
            tmp = self.state_path.with_name(STATE_FILE + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, sort_keys=True, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
