"""执行环境画像与能力门禁：本地/CI 结论必须一致。

关键约束：
- 影响产物的环境变量经固定白名单归一化后进入缓存键；白名单之外的变量变化不影响缓存，
  白名单之内的任何变化必然改变缓存键。
- profile（local/ci）只是元数据，绝不改变版本/缓存/健康判定逻辑。
- 被请求但当前环境不支持的能力，返回不支持原因，由调用方显式拒绝，不得静默忽略。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys

from .models import CapabilityIssue, EnvProfile

# 影响构建产物、允许进入缓存键的环境变量白名单（值经排序规范化，大小写敏感）
CACHE_ENV_WHITELIST: list[str] = [
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "UV_INDEX_URL",
    "UV_HTTP_TIMEOUT",
    "RELEASE_BUILD_PROFILE",
    "RELEASE_TARGET_ARCH",
    "RELEASE_TARGET_OS",
]

# 主流 CI 系统的判定变量（仅用于标记来源，不参与判定逻辑）
_CI_MARKERS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "JENKINS_URL",
    "CIRCLECI",
    "BUILDKITE",
    "TF_BUILD",
    "TRAVIS",
)


def detect_profile(environ: dict[str, str] | None = None) -> EnvProfile:
    env = os.environ if environ is None else environ

    ci_hits = sorted(k for k in _CI_MARKERS if env.get(k))
    is_ci = bool(ci_hits)
    profile_name = "ci" if is_ci else "local"
    ci_provider = _provider_name(env) if is_ci else None

    git_ok = shutil.which("git") is not None
    caps = [
        CapabilityIssue(
            capability="git",
            supported=git_ok,
            reason="可用" if git_ok else "PATH 中无 git：版本推导将降级为无历史模式并在结论中说明",
        ),
        CapabilityIssue(
            capability="filesystem-lock",
            supported=sys.platform != "win32",
            reason=(
                "可用（fcntl 排他锁）"
                if sys.platform != "win32"
                else "不支持：Windows 无 fcntl，无法保证并发安全，拒绝执行并发路径"
            ),
        ),
        CapabilityIssue(
            capability="atomic-rename",
            supported=True,
            reason="可用（同目录 os.replace + fsync）",
        ),
    ]

    return EnvProfile(
        profile=profile_name,
        ci_provider=ci_provider,
        python_version=platform.python_version(),
        git_available=git_ok,
        capabilities=tuple(caps),
        normalized=normalize_environment(env),
    )


def _provider_name(env: dict[str, str]) -> str:
    if env.get("GITHUB_ACTIONS"):
        return "github-actions"
    if env.get("GITLAB_CI"):
        return "gitlab-ci"
    if env.get("JENKINS_URL"):
        return "jenkins"
    if env.get("CIRCLECI"):
        return "circleci"
    if env.get("BUILDKITE"):
        return "buildkite"
    if env.get("TF_BUILD"):
        return "azure-pipelines"
    if env.get("TRAVIS"):
        return "travis"
    return "generic-ci"


def normalize_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """抽取白名单变量；本地与 CI 同一输入产生同一环境摘要。"""
    env = os.environ if environ is None else environ
    return {key: env.get(key, "") for key in CACHE_ENV_WHITELIST}


def enforce_capabilities(
    profile: EnvProfile, *, require_cache: bool, require_rollback: bool
) -> tuple[CapabilityIssue, ...]:
    """校验被请求的能力是否被支持；返回所有不支持项（supported=False）。

    缓存与本地回滚只依赖文件系统/原子重命名；在标准 POSIX 环境始终可用。
    任何后续新增的可选能力在此统一登记，缺失即拒绝，而不是静默降级到错误行为。
    """
    rejected = tuple(c for c in profile.capabilities if not c.supported)
    if require_rollback:
        # 回滚依赖原子指针切换，需要 atomic-rename 能力
        if not any(
            c.capability == "atomic-rename" and c.supported
            for c in profile.capabilities
        ):
            rejected += (
                CapabilityIssue(
                    capability="rollback",
                    supported=False,
                    reason="缺少 atomic-rename 能力，无法保证回滚原子性，拒绝上线",
                ),
            )
    if require_cache:
        if not any(
            c.capability == "filesystem-lock" and c.supported
            for c in profile.capabilities
        ):
            rejected += (
                CapabilityIssue(
                    capability="cache",
                    supported=False,
                    reason="缺少文件锁能力，无法保证并发缓存安全，拒绝使用缓存",
                ),
            )
    return rejected
