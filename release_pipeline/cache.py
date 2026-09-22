"""构建缓存：键覆盖所有影响产物的输入，损坏降级重建。

缓存键::

    sha256(target | version | spec_digest | sources_digest | env_digest)

- spec_digest：构建命令与选项（命令变 -> 必失效）
- sources_digest：构建输入文件的路径+内容摘要（内容变 -> 必失效）
- env_digest：白名单环境变量的规范化摘要（环境变 -> 必失效）
- version：版本推导结论（含 dirty/tree 摘要）

命中后仍需校验：条目元数据完整、产物存在、产物 sha256 与记录一致。
任何不一致视为损坏：隔离到 quarantine/ 后重建，绝不产出错误结果。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .locking import FileLock, read_json, write_json_atomic
from .models import CacheStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheEntry:
    cache_key: str
    target: str
    version: str
    artifact_path: str
    artifact_digest: str
    env_digest: str
    spec_digest: str
    sources_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "cache_key": self.cache_key,
            "target": self.target,
            "version": self.version,
            "artifact_path": self.artifact_path,
            "artifact_digest": self.artifact_digest,
            "env_digest": self.env_digest,
            "spec_digest": self.spec_digest,
            "sources_digest": self.sources_digest,
        }

    @classmethod
    def from_dict(cls, data: object) -> "CacheEntry":
        if not isinstance(data, dict):
            raise ValueError("缓存条目不是 JSON 对象")
        required = (
            "cache_key",
            "target",
            "version",
            "artifact_path",
            "artifact_digest",
            "env_digest",
            "spec_digest",
            "sources_digest",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(f"缓存条目缺少字段: {missing}")
        if any(not isinstance(data[k], str) for k in required):
            raise ValueError("缓存条目存在非字符串字段")
        return cls(**{k: data[k] for k in required})


def compute_cache_key(
    *,
    target: str,
    version: str,
    spec_digest: str,
    sources_digest: str,
    env_digest: str,
) -> str:
    """缓存键 = sha256(目标, 版本, 构建规格, 输入源摘要, 环境摘要)。"""
    h = hashlib.sha256()
    for part in (target, version, spec_digest, sources_digest, env_digest):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def digest_paths(paths: list[Path], *, base: Path) -> str:
    """对输入文件/目录做内容摘要（相对路径排序后逐个哈希）。

    缺失路径直接抛错——输入声明不完整会导致错误复用，必须显式失败。
    """
    base = Path(base).resolve()
    collected: list[Path] = []
    for raw in paths:
        p = Path(raw)
        p = p if p.is_absolute() else base / p
        if not p.exists():
            raise FileNotFoundError(f"缓存输入不存在: {p}（拒绝在输入不明时构建/取缓存）")
        if p.is_dir():
            collected.extend(q for q in sorted(p.rglob("*")) if q.is_file())
        else:
            collected.append(p)

    h = hashlib.sha256()
    for p in sorted(collected):
        rel = p.resolve().relative_to(base).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        fhash = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                fhash.update(chunk)
        h.update(fhash.hexdigest().encode("ascii"))
        h.update(b"\x00")
    return h.hexdigest()


def digest_environment(env: dict[str, str], *, allowed: list[str]) -> str:
    """仅纳入白名单环境变量，避免无关变量导致抖动，也不遗漏影响产物的变量。"""
    h = hashlib.sha256()
    for key in sorted(allowed):
        h.update(key.encode("utf-8"))
        h.update(b"=")
        h.update(str(env.get(key, "")).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class BuildCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.artifacts_dir = self.root / "artifacts"
        self.quarantine_dir = self.root / "quarantine"
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    def _entry_path(self, cache_key: str) -> Path:
        return self.entries_dir / f"{cache_key}.json"

    def _lock_path(self, cache_key: str) -> Path:
        return self.root / "locks" / f"{cache_key}.lock"

    def lookup(self, cache_key: str) -> tuple[CacheEntry | None, CacheStatus]:
        """返回 (条目, 判定)。

        判定随返回值给出而不是放在共享实例属性上：多线程并发查询同一 cache
        对象时不会把 A 目标的 hit/miss/corrupt 错记到 B 目标上。
        命中且完整返回 (entry, HIT)；条目不存在 => (None, MISS)；
        损坏条目隔离后 => (None, CORRUPT_REBUILT)，调用方按 miss 重建。
        """
        path = self._entry_path(cache_key)
        if not path.exists():
            logger.info("cache lookup target-entry=%s 判定=MISS 依据=条目不存在", cache_key[:12])
            return None, CacheStatus.MISS
        try:
            data = read_json(path)
            entry = CacheEntry.from_dict(data)
            if entry.cache_key != cache_key:
                raise ValueError("条目 cache_key 与文件名不一致")
            artifact = Path(entry.artifact_path)
            if not artifact.is_file():
                raise ValueError(f"产物缺失: {artifact}")
            actual = _sha256_file(artifact)
            if actual != entry.artifact_digest:
                raise ValueError(
                    f"产物摘要不匹配: 记录={entry.artifact_digest[:12]} 实际={actual[:12]}"
                )
        except (ValueError, OSError) as exc:
            logger.warning(
                "cache lookup target-entry=%s 判定=CORRUPT 依据=%s -> 隔离并重建",
                cache_key[:12],
                exc,
            )
            self._quarantine(cache_key, path, reason=str(exc))
            return None, CacheStatus.CORRUPT_REBUILT

        logger.info(
            "cache lookup target-entry=%s 判定=HIT 依据=元数据完整且产物 sha256 一致",
            cache_key[:12],
        )
        return entry, CacheStatus.HIT

    def store(self, entry: CacheEntry) -> None:
        """原子写入条目与产物；相同键并发存储不得半更新或错配。"""
        with FileLock(self._lock_path(entry.cache_key), timeout=30.0):
            dest_artifact = self.artifacts_dir / f"{entry.cache_key}.bin"
            src = Path(entry.artifact_path)
            tmp_artifact = dest_artifact.with_suffix(".bin.tmp")
            try:
                if src.resolve() != dest_artifact.resolve():
                    shutil.copyfile(src, tmp_artifact)
                    os.replace(tmp_artifact, dest_artifact)
                stored = CacheEntry(
                    cache_key=entry.cache_key,
                    target=entry.target,
                    version=entry.version,
                    artifact_path=str(dest_artifact),
                    artifact_digest=entry.artifact_digest,
                    env_digest=entry.env_digest,
                    spec_digest=entry.spec_digest,
                    sources_digest=entry.sources_digest,
                )
                write_json_atomic(self._entry_path(entry.cache_key), stored.to_dict())
                logger.info(
                    "cache store target=%s entry=%s 完成（元数据+产物均已落盘）",
                    entry.target,
                    entry.cache_key[:12],
                )
            finally:
                if tmp_artifact.exists() and src.resolve() != dest_artifact.resolve():
                    tmp_artifact.unlink(missing_ok=True)

    def _quarantine(self, cache_key: str, entry_path: Path, *, reason: str) -> None:
        stamp_dir = self.quarantine_dir / cache_key[:16]
        stamp_dir.mkdir(parents=True, exist_ok=True)
        try:
            if entry_path.exists():
                os.replace(entry_path, stamp_dir / "entry.json")
            artifact = self.artifacts_dir / f"{cache_key}.bin"
            if artifact.exists():
                os.replace(artifact, stamp_dir / "artifact.bin")
            (stamp_dir / "reason.txt").write_text(reason, encoding="utf-8")
        except OSError as exc:  # 隔离失败不致命：直接删除损坏条目
            logger.warning("隔离损坏缓存失败，改为直接删除: %s", exc)
            entry_path.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
