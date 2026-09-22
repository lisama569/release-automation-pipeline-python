"""文件锁、原子写与临时资源管理：保证并发与中断安全。

设计要点：
- FileLock 基于 fcntl.flock，跨进程互斥；带超时，拿不到锁明确报错而不是死等。
- atomic_write 走“同目录临时文件 + fsync + os.replace”，读者只会看到完整内容或旧内容。
- temp_workdir 退出即清理；sweep_stale 供重试开始时清扫上次硬中断遗留的临时目录。
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import PipelineError


class LockTimeout(PipelineError):
    """等待文件锁超时。"""


class FileLock:
    """基于目录内 flock 的跨进程互斥锁。"""

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fh: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh = fh
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fh)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fh)
                    raise LockTimeout(
                        f"等待锁超时: {self.path}",
                        details={"lock": str(self.path), "timeout_s": self.timeout},
                    ) from exc
                time.sleep(0.02)

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                os.close(self._fh)
                self._fh = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _fsync_dir(path: Path) -> None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def atomic_write(path: Path) -> Iterator[Any]:
    """以临时文件 + fsync + 原子 rename 写入，避免半更新。

    用法::

        with atomic_write(p) as fh:
            fh.write(data)
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def write_json_atomic(path: Path, payload: Any) -> None:
    """原子写入 JSON（sort_keys 保证同一对象字节一致，便于复现与比对）。"""
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)
    with atomic_write(path) as fh:
        fh.write(data)
        fh.write("\n")


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


@contextlib.contextmanager
def temp_workdir(base: Path, prefix: str = "tmp-") -> Iterator[Path]:
    """临时工作目录；无论成功失败都清理，异常退出后重试不留垃圾。"""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=str(base), prefix=prefix))
    try:
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)


def sweep_stale(base: Path, prefix: str = "tmp-") -> list[str]:
    """清理上次硬中断遗留的临时目录（按前缀精确匹配，绝不误碰其他条目）。"""
    base = Path(base)
    removed: list[str] = []
    if not base.exists():
        return removed
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith(prefix):
            shutil.rmtree(child, ignore_errors=True)
            removed.append(str(child))
    return removed
