"""本地模拟的部署目标环境（文件系统即环境，无需外部服务）。

目录布局::

    <env_root>/<target>/
        versions/<v>/payload      # 解包后的业务产物
        versions/<v>/health.json  # 随产物构建的健康清单
        current -> versions/<v>   # 对外可见版本（原子符号链接）

构建产物统一为 tar bundle（内含 payload 与 health.json）。stage 时完整解包到临时
目录再原子 rename，中断重试不会留下半个版本目录。对外可见状态永远只由 current 指针
决定，promote/rollback 都通过“临时链接 + os.replace”原子切换。
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
from pathlib import Path

from .errors import RolloutError

logger = logging.getLogger(__name__)
_BUNDLE_MEMBERS = {"payload", "health.json"}
_BUNDLE_REQUIRED = {"payload"}  # health.json 缺失本身是一种合法的“健康结论不明确”信号


class LocalTargetEnvironment:
    def __init__(self, root: Path, target: str) -> None:
        self.root = Path(root) / target
        self.target = target
        self.versions_dir = self.root / "versions"
        self.current_link = self.root / "current"
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def current_version(self) -> str | None:
        link = self.current_link
        if link.is_symlink():
            return os.readlink(link).rstrip("/").split("/")[-1]
        if link.exists():
            raise RolloutError(
                f"目标 {self.target} 的 current 不是符号链接，环境状态不明确",
                details={"target": self.target, "path": str(link)},
            )
        return None

    def stage(self, version: str, artifact: Path) -> None:
        """暂存版本（解包 tar bundle），不改变 current 指针；同版本重复暂存需内容一致。"""
        artifact = Path(artifact)
        if not artifact.is_file():
            raise RolloutError(
                f"暂存失败：产物不存在 {artifact}",
                details={"target": self.target, "version": version},
            )
        dest_dir = self.versions_dir / version
        if dest_dir.exists():
            if _bundle_matches(artifact, dest_dir):
                return
            if self.current_version == version:
                raise RolloutError(
                    f"目标 {self.target} 正在对外服务版本 {version}，且新产物内容不同，"
                    "拒绝覆盖线上版本",
                    details={"target": self.target, "version": version},
                )
            # 仅暂存、未被 current 指向：上轮失败遗留的同号版本，可原子替换为修复后的包
            logger.info(
                "target=%s 版本=%s 的已暂存内容与新产物不同（未对外服务），原子替换",
                self.target, version,
            )
            import shutil as _shutil

            _shutil.rmtree(dest_dir)
        tmp_dir = self.versions_dir / f".{version}.stage-tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)
        try:
            _safe_extract(artifact, tmp_dir)
            os.replace(tmp_dir, dest_dir)
            logger.info("target=%s stage 版本=%s 完成（current 未变更）", self.target, version)
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    def promote(self, version: str) -> None:
        """把 current 指针原子切到指定版本。"""
        self._require_complete(version)
        self._swap_link(version)
        logger.info("target=%s promote -> %s", self.target, version)

    def rollback_to(self, version: str) -> None:
        """回滚到指定已存在版本；版本缺失时明确失败（不得伪造对外状态）。"""
        self._require_complete(version)
        self._swap_link(version)
        logger.info("target=%s rollback -> %s", self.target, version)

    def clear_staged(self) -> None:
        """清理中断遗留的暂存临时目录，不触碰任何已发布版本。"""
        for child in self.versions_dir.iterdir():
            if child.is_dir() and child.name.startswith(".") and child.name.endswith(".stage-tmp"):
                shutil.rmtree(child, ignore_errors=True)
                logger.info("target=%s 清理遗留暂存目录 %s", self.target, child.name)

    def _require_complete(self, version: str) -> None:
        dest_dir = self.versions_dir / version
        # payload 必填；health.json 允许缺失——探针会据此给出“不明确”结论而不是放行
        if not (dest_dir / "payload").is_file():
            raise RolloutError(
                f"版本 {version} 产物不完整（缺少 payload），拒绝切换",
                details={"target": self.target, "version": version},
            )

    def _swap_link(self, version: str) -> None:
        tmp_link = self.root / ".current.tmp"
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        os.symlink(f"versions/{version}", tmp_link)
        os.replace(tmp_link, self.current_link)


def _safe_extract(bundle: Path, dest: Path) -> None:
    """解包 bundle，仅允许固定成员名，拒绝路径穿越。"""
    dest = dest.resolve()
    with tarfile.open(bundle, mode="r:") as tar:
        names = set(tar.getnames())
        if not _BUNDLE_REQUIRED.issubset(names) or not names.issubset(_BUNDLE_MEMBERS):
            raise RolloutError(
                f"产物 bundle 成员非法：{sorted(names)}，期望为 "
                f"{sorted(_BUNDLE_MEMBERS)} 的子集且包含 {sorted(_BUNDLE_REQUIRED)}",
                details={"bundle": str(bundle)},
            )
        for member in tar.getmembers():
            if not member.isfile() or member.name not in _BUNDLE_MEMBERS:
                raise RolloutError(f"产物 bundle 含非法成员: {member.name!r}")
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest) + os.sep):
                raise RolloutError(f"产物 bundle 路径穿越: {member.name!r}")
            src = tar.extractfile(member)
            if src is None:
                raise RolloutError(f"产物 bundle 成员无法读取: {member.name!r}")
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _bundle_matches(bundle: Path, dest_dir: Path) -> bool:
    """已暂存同版本时，逐成员比较 bundle 内容与现有版本目录是否一致。"""
    with tarfile.open(bundle, mode="r:") as tar:
        if set(tar.getnames()) != _BUNDLE_MEMBERS:
            return False
        for name in sorted(_BUNDLE_MEMBERS):
            p = dest_dir / name
            if not p.is_file():
                return False
            try:
                member_file = tar.extractfile(name)
            except KeyError:
                return False
            if member_file is None:
                return False
            if member_file.read() != p.read_bytes():
                return False
    return True
