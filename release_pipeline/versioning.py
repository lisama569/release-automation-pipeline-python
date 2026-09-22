"""版本与变更记录推导：确定性、可复现、来源冲突可解释。

版本规则（全部为合法 PEP 440）：
1. 版本来源优先级：显式参数/RELEASE_VERSION 环境变量 > VERSION 文件 > 最近语义化标签 > 0.0.0。
2. 若当前提交恰好被语义化标签命中（tag on HEAD），采用该标签版本；与更高优先级来源冲突时，
   保留高优先级裁决，但在 resolution 中记录冲突，绝不静默。
3. 非发布提交：``<base>.dev<N>``，N = base_tag..HEAD 的提交数（无提交时为 0）。
4. 工作区有未提交修改：追加 ``+dirty.<digest12>``。
5. 无提交（unborn HEAD）：追加 ``+tree.<digest12>`` 以区分不同初始工作区。

相同仓库状态 + 相同输入，永远得到相同版本。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from .errors import VersionError
from .models import VersionInfo
from .vcs import GitInspector, RepoState

logger = logging.getLogger(__name__)

_PEP440_LOCAL = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?$")
_EXPLICIT_RE = re.compile(r"^[vV]?\d+\.\d+\.\d+$")


def normalize_version(raw: str) -> str:
    """把任意来源的原始版本规范化为 PEP 440 核心版本；非法时抛 VersionError。"""
    text = raw.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    text = text.lstrip("=")
    if not _VERSION_RE.match(text):
        raise VersionError(
            f"非法版本号: {raw!r}（期望形如 1.2.3 / 1.2.3.dev1）",
            details={"raw": raw},
        )
    return text


def _short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def derive_version(
    repo_root: Path,
    *,
    explicit_version: str | None = None,
    version_file: str = "VERSION",
) -> VersionInfo:
    """从仓库状态推导版本。相同输入永远得到相同结论。"""
    repo_root = Path(repo_root)
    state = GitInspector(repo_root).inspect()

    sources: dict[str, str] = {}
    notes: list[str] = []

    explicit = _read_explicit(explicit_version, sources)
    file_ver = _read_version_file(repo_root / version_file, sources, notes)
    tag_ver = state.base_version
    if tag_ver:
        sources["git-tag"] = f"{state.base_tag}->{tag_ver}"

    base_source: str
    base_version: str
    if explicit is not None:
        base_source = "explicit"
        base_version = explicit
    elif file_ver is not None:
        base_source = f"file:{version_file}"
        base_version = file_ver
    elif tag_ver is not None:
        base_source = "git-tag"
        base_version = tag_ver
    else:
        base_source = "default"
        base_version = "0.0.0"
        sources.setdefault("default", "0.0.0")
        if not state.available:
            notes.append(state.reason or "git 不可用")
            notes.append("历史信息缺失：回退到默认基础版本 0.0.0，结论仍由工作区摘要确定")
        elif state.head_commit is None:
            notes.append("历史信息缺失：仓库尚无提交（unborn HEAD），回退到默认基础版本 0.0.0，结论由工作区摘要确定")

    # 冲突检测：被采纳来源与 tag/文件不一致时必须记录，不得静默取一个
    for other_src, other_val in (("file", file_ver), ("git-tag", tag_ver)):
        if other_val is not None and other_val != base_version:
            notes.append(
                f"来源冲突：{other_src}={other_val} 与 {base_source}={base_version} 不一致；"
                f"按优先级采用 {base_source}"
            )

    on_head_tag = state.tags_on_head
    head_ver = None
    if on_head_tag:
        from .vcs import _tag_to_version

        semver_tags = sorted(t for t in on_head_tag if _tag_to_version(t))
        if semver_tags:
            head_tag = semver_tags[0]
            head_ver = _tag_to_version(head_tag)
            if len(semver_tags) > 1:
                notes.append(
                    f"HEAD 上存在多个语义化标签 {semver_tags}；确定性选取字典序最小者 {head_tag}"
                )

    version = base_version
    is_tagged_release = bool(
        head_ver is not None
        and base_source == "git-tag"
        and state.base_tag in on_head_tag
        and not state.dirty
    )

    if not is_tagged_release:
        n = len(state.commits)
        if state.head_commit is None:
            n = 0  # unborn：尚无提交
        # 已在版本中的 dev 后缀不重复追加
        if ".dev" not in version:
            version = f"{version}.dev{n}"
        if head_ver is not None and base_version != head_ver:
            notes.append(
                f"HEAD 标签 {head_ver} 与采用的基础版本 {base_version} 不一致，"
                f"按 {base_source} 裁决，标签不构成发布"
            )

    local_parts: list[str] = []
    if state.dirty:
        local_parts.append(f"dirty.{_short(state.dirty_digest or 'dirty')}")
    if state.head_commit is None and state.tracked_digest:
        local_parts.append(f"tree.{_short(state.tracked_digest)}")
    if local_parts:
        version = f"{version}+{'.'.join(local_parts)}"
    if not _is_pep440(version):
        raise VersionError(f"内部错误：推导出版本 {version} 不符合 PEP 440")

    resolution = "; ".join(notes) if notes else f"采用 {base_source}={base_version}，无冲突"
    changelog = _build_changelog(state, notes)

    info = VersionInfo(
        version=version,
        commit=state.head_commit,
        source=base_source,
        resolution=resolution,
        sources=sources,
        dirty=state.dirty,
        dirty_digest=state.dirty_digest,
        tree_digest=state.tracked_digest,
        changelog=changelog,
    )
    logger.info(
        "version 推导结论 version=%s source=%s commit=%s dirty=%s resolution=%s",
        info.version,
        info.source,
        info.commit,
        info.dirty,
        info.resolution,
    )
    return info


def _read_explicit(raw: str | None, sources: dict[str, str]) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        raise VersionError("显式版本为空字符串，拒绝推导", details={"explicit": raw})
    if not _EXPLICIT_RE.match(text):
        raise VersionError(
            f"显式版本 {raw!r} 非 X.Y.Z 形式，拒绝推导（避免环境注入任意字符串）",
            details={"explicit": raw},
        )
    norm = normalize_version(text)
    sources["explicit"] = norm
    return norm


def _read_version_file(
    path: Path, sources: dict[str, str], notes: list[str]
) -> str | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VersionError(
            f"VERSION 文件不可读: {exc}", details={"path": str(path)}
        ) from exc
    try:
        norm = normalize_version(raw)
    except VersionError as exc:
        raise VersionError(
            f"{path.name} 中版本非法: {exc.reason}", details={"path": str(path)}
        ) from exc
    sources[f"file:{path.name}"] = norm
    return norm


def _is_pep440(version: str) -> bool:
    core, _, local = version.partition("+")
    if local and not _PEP440_LOCAL.match(local):
        return False
    return bool(_VERSION_RE.match(core))


def _build_changelog(state: RepoState, notes: list[str]) -> tuple[str, ...]:
    entries: list[str] = []
    if state.available:
        if state.base_tag:
            entries.append(f"基线标签: {state.base_tag}")
        else:
            entries.append("基线标签: 无（历史信息缺失，变更范围按全部提交计）")
        if state.commits:
            entries.extend(f"- {c}" for c in state.commits)
        else:
            entries.append("- （基线之后无提交）")
        if state.dirty:
            entries.append("- [未提交改动] 工作区相对 HEAD 有差异（见 dirty 摘要）")
    else:
        entries.append("git 不可用：无法生成提交变更记录（已在版本 resolution 中说明降级依据）")
    entries.extend(f"[裁决] {n}" for n in notes)
    return tuple(entries)
