"""流水线跨模块共享的数据模型（仅类型定义，不含业务逻辑）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TargetStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # 因依赖目标失败而跳过


class MatrixConclusion(str, Enum):
    SUCCESS = "success"           # 全部目标成功
    PARTIAL_FAILURE = "partial_failure"  # 有成功也有失败
    FAILURE = "failure"           # 全部失败
    EMPTY = "empty"               # 没有任何目标


class CacheStatus(str, Enum):
    HIT = "hit"
    MISS = "miss"
    CORRUPT_REBUILT = "corrupt_rebuilt"  # 命中但损坏，已降级重建


class ReleaseStage(str, Enum):
    BUILD = "build"
    CANARY = "canary"
    PARTIAL = "partial"
    FULL = "full"


class ReleaseState(str, Enum):
    """发布状态机：对外可见的上线状态。"""
    ABSENT = "absent"
    DEPLOYED = "deployed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class VersionInfo:
    """推导得到的版本结论。version 为规范化后的 PEP 440 版本。"""
    version: str
    commit: str | None
    source: str                 # 最终采用的版本来源标识
    resolution: str             # 冲突/缺失时的裁决说明
    sources: dict[str, str] = field(default_factory=dict)  # 各来源原始值
    dirty: bool = False
    dirty_digest: str | None = None
    tree_digest: str | None = None
    changelog: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildSpec:
    """单个构建目标的规格。"""
    target: str
    command: str
    inputs: tuple[str, ...] = ()
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BuildResult:
    target: str
    status: TargetStatus
    cache_status: CacheStatus | None
    version: str
    cache_key: str | None
    artifact_path: str | None
    artifact_digest: str | None
    log: str
    duration_ms: int


@dataclass(frozen=True)
class MatrixReport:
    results: tuple[BuildResult, ...]
    conclusion: MatrixConclusion
    successful: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def exit_ok(self) -> bool:
        """全部目标成功才算通过；部分失败、全失败、空矩阵均为 False。"""
        return self.conclusion is MatrixConclusion.SUCCESS


@dataclass(frozen=True)
class HealthProbe:
    target: str
    healthy: bool
    ambiguous: bool
    detail: str


@dataclass(frozen=True)
class StageResult:
    stage: ReleaseStage
    state: ReleaseState
    probes: tuple[HealthProbe, ...]
    rolled_back: bool


@dataclass(frozen=True)
class RolloutReport:
    version: str
    reached_stage: ReleaseStage | None
    final_state: ReleaseState
    rolled_back: bool
    stages: tuple[StageResult, ...]
    reason: str | None


@dataclass(frozen=True)
class CapabilityIssue:
    capability: str
    supported: bool
    reason: str


@dataclass(frozen=True)
class EnvProfile:
    """本地/CI 执行环境画像。"""
    profile: str                # "local" | "ci"
    ci_provider: str | None
    python_version: str
    git_available: bool
    capabilities: tuple[CapabilityIssue, ...]
    normalized: dict[str, str] = field(default_factory=dict)
