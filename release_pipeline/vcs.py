"""Git 仓库状态读取；git 缺失/非仓库时给出可区分的降级结论。

所有输出只取决于仓库内容：相同仓库状态永远产生相同摘要。不依赖网络、远程、
凭据或 CI 注入的元数据。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoState:
    repo_root: Path | None
    head_commit: str | None
    branch: str | None
    tags_on_head: tuple[str, ...]
    base_tag: str | None
    base_version: str | None
    commits: tuple[str, ...]          # base_tag..HEAD 的提交摘要
    dirty: bool
    tracked_digest: str | None        # HEAD 树内容摘要
    dirty_digest: str | None          # 工作区相对树的增量摘要
    available: bool
    reason: str | None


def _git_env() -> dict[str, str]:
    import os

    env = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    for key in ("SYSTEMROOT",):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _hash_files(root: Path, rel_paths: list[str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(rel_paths):
        full = root / rel
        if full.is_file():
            with full.open("rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
        h.update(b"\x00")
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _hash_working_tree(root: Path) -> str:
    """无提交时的降级摘要：对工作区全部非 .git 文件做内容哈希。"""
    rels: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        rels.append(rel)
    return _hash_files(root, rels)


class GitInspector:
    def __init__(self, path: Path, *, git_bin: str = "git") -> None:
        self.path = Path(path)
        self.git_bin = git_bin

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_bin, "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=_git_env(),
            check=False,
        )

    def inspect(self) -> RepoState:
        if shutil.which(self.git_bin) is None:
            return self._unavailable("git 不可用：PATH 中未找到 git 二进制，无法读取仓库历史")

        top = self._run("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            reason = top.stderr.strip().splitlines()[-1] if top.stderr.strip() else "非 git 仓库"
            return self._unavailable(f"git 不可用：{reason}")

        root = Path(top.stdout.strip())
        head_p = self._run("rev-parse", "HEAD")
        unborn = head_p.returncode != 0
        head_commit = None if unborn else head_p.stdout.strip()

        branch = None
        br = self._run("rev-parse", "--abbrev-ref", "HEAD")
        if br.returncode == 0:
            name = br.stdout.strip()
            branch = None if name == "HEAD" else name

        tags_p = self._run("tag", "--points-at", "HEAD")
        tags_on_head = tuple(sorted(t for t in tags_p.stdout.splitlines() if t))

        base_tag, base_version = self._resolve_base_tag(root)

        if unborn:
            commits: tuple[str, ...] = ()
            tracked_digest: str | None = _hash_working_tree(root)
        else:
            rng = f"{base_tag}..HEAD" if base_tag else "HEAD"
            log = self._run("log", "--pretty=format:%h %s", rng)
            commits = tuple(log.stdout.splitlines()) if log.returncode == 0 else ()
            tree = self._run("rev-parse", "HEAD^{tree}")
            tracked_digest = tree.stdout.strip() if tree.returncode == 0 else _hash_working_tree(root)

        dirty, dirty_digest = self._dirty_state(root)

        return RepoState(
            repo_root=root,
            head_commit=head_commit,
            branch=branch,
            tags_on_head=tags_on_head,
            base_tag=base_tag,
            base_version=base_version,
            commits=commits,
            dirty=dirty,
            tracked_digest=tracked_digest,
            dirty_digest=dirty_digest,
            available=True,
            reason=None,
        )

    def _unavailable(self, reason: str) -> RepoState:
        # 即使 git 不可用，也基于目录内容给出确定性摘要：
        # 相同内容仍可复现，不同内容不会产生同一版本
        try:
            tracked_digest = _hash_working_tree(self.path)
        except OSError:
            tracked_digest = None
        return RepoState(
            repo_root=None,
            head_commit=None,
            branch=None,
            tags_on_head=(),
            base_tag=None,
            base_version=None,
            commits=(),
            dirty=False,
            tracked_digest=tracked_digest,
            dirty_digest=None,
            available=False,
            reason=reason,
        )

    def _resolve_base_tag(self, root: Path) -> tuple[str | None, str | None]:
        """选择 HEAD 可达的最高语义化版本标签；结果确定性（版本序+文件名字典序兜底）。"""
        tagged = self._run("tag", "--list")
        if tagged.returncode != 0:
            return None, None
        candidates: list[tuple[tuple[int, ...], str, str]] = []
        for raw in tagged.stdout.splitlines():
            ver = _tag_to_version(raw)
            if ver is None:
                continue
            anc = self._run("merge-base", "--is-ancestor", raw, "HEAD")
            if anc.returncode != 0:
                continue
            key = tuple(int(x) for x in ver.split("."))
            candidates.append((key, raw, ver))
        if not candidates:
            return None, None
        # 最高版本优先；同版本（如 v1 与 1）按标签名字典序，保证唯一裁决
        candidates.sort(key=lambda c: (c[0], c[1]))
        _, tag, ver = candidates[-1]
        return tag, ver

    def _dirty_state(self, root: Path) -> tuple[bool, str | None]:
        porcelain = self._run("status", "--porcelain=v1", "--untracked-files=all", "--no-renames")
        lines = sorted(line for line in porcelain.stdout.splitlines() if line)
        if not lines:
            return False, None
        import hashlib

        h = hashlib.sha256()
        for line in lines:
            status = line[:2]
            rel = line[3:]
            full = root / rel
            if full.is_file():
                with full.open("rb") as fh:
                    content_hash = hashlib.sha256(fh.read()).hexdigest()
            else:
                content_hash = "absent"
            h.update(status.encode("utf-8"))
            h.update(b"\x00")
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            h.update(content_hash.encode("ascii"))
            h.update(b"\x00")
        return True, h.hexdigest()


def _tag_to_version(tag: str) -> str | None:
    """v1.2.3 / 1.2.3 -> 1.2.3；非纯数字三段标签返回 None（不猜测）。"""
    raw = tag[1:] if tag.startswith("v") else tag
    parts = raw.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return raw
