"""Shared immutable data types for the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TargetStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class RolloutStage(str, Enum):
    NONE = "none"
    STAGING = "staging"
    CANARY = "canary"
    FULL = "full"


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    short_sha: str
    subject: str
    commit_time: str


@dataclass(frozen=True)
class VersionInfo:
    version: str
    commit: str
    dirty: bool
    commit_count: int
    source: str
    reproducible_hash: str
    changelog: tuple[tuple[str, str], ...] = field(default=())


@dataclass(frozen=True)
class BuildTarget:
    name: str
    artifact_name: str
    fail: bool = False


@dataclass(frozen=True)
class BuildInput:
    """Everything that can influence a target's artifact."""

    target: BuildTarget
    version: str
    source_hash: str
    env_fingerprint: str
    config_fingerprint: str
    toolchain: str


@dataclass(frozen=True)
class Artifact:
    target: str
    version: str
    path: str
    content_hash: str
    cache_hit: bool
    cache_key: str


@dataclass(frozen=True)
class TargetResult:
    target: str
    status: TargetStatus
    artifact: Artifact | None
    reason: str = ""


@dataclass(frozen=True)
class BuildReport:
    version: str
    cache_key: str
    results: tuple[TargetResult, ...]

    @property
    def succeeded(self) -> tuple[TargetResult, ...]:
        return tuple(r for r in self.results if r.status is TargetStatus.SUCCESS)

    @property
    def failed(self) -> tuple[TargetResult, ...]:
        return tuple(r for r in self.results if r.status is TargetStatus.FAILED)

    @property
    def all_succeeded(self) -> bool:
        return bool(self.results) and not self.failed


@dataclass(frozen=True)
class HealthVerdict:
    healthy: bool
    conclusive: bool
    detail: str


@dataclass(frozen=True)
class DeployResult:
    version: str
    stage: RolloutStage
    rolled_back: bool
    previous_version: str | None
    visible_version: str | None
    reason: str
