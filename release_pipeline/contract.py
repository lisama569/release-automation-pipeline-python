"""Request-contract validation shared by execution and preview.

Every violation raises :class:`ContractViolationError` with a distinct
machine-readable ``code`` *before* any directory is created or any state is
touched, so an invalid request can never be partially executed.
"""

from __future__ import annotations

from dataclasses import dataclass

# Canonical monotonic rollout chain. A legal stage sequence is exactly one
# contiguous prefix of this chain (1..3 entries).
STAGE_CHAIN: tuple[str, ...] = ("staging", "canary", "full")

# Targets this pipeline knows how to build. Anything else is rejected with
# ``unknown_target`` so a typo can never be silently "partially executed".
DEFAULT_KNOWN_TARGETS: tuple[str, ...] = (
    "linux-x86_64",
    "linux-aarch64",
    "darwin-x86_64",
    "darwin-aarch64",
    "windows-x86_64",
)


@dataclass(frozen=True)
class ValidatedRequest:
    """The normalized, contract-clean view of a pipeline request."""

    targets: tuple[str, ...]
    stages: tuple[str, ...]
    failing_targets: frozenset[str]


def validate_request(
    *,
    targets: tuple[str, ...],
    stages: tuple[str, ...],
    failing_targets: frozenset[str] = frozenset(),
    known_targets: tuple[str, ...] = DEFAULT_KNOWN_TARGETS,
) -> ValidatedRequest:
    """Validate a request; raise ContractViolationError on any violation.

    The checks are ordered so the *first* violation determines the code:

    * ``empty_targets``      — no build targets requested
    * ``duplicate_target``   — the same target requested twice
    * ``unknown_target``     — target not in ``known_targets``
    * ``unknown_fail_target``— fail-injection for a target not requested
    * ``empty_stages``       — no rollout stages requested
    * ``unknown_stage``      — stage not in the canonical chain
    * ``invalid_stage_order``— not a contiguous monotonic prefix of the chain
    """
    from .errors import ContractViolationError

    if not targets:
        raise ContractViolationError(
            "no build targets requested; refusing to run an empty matrix",
            code="empty_targets",
        )
    seen: set[str] = set()
    for name in targets:
        if name in seen:
            raise ContractViolationError(
                f"duplicate target {name!r} in request",
                code="duplicate_target",
            )
        seen.add(name)
    unknown_targets = [t for t in targets if t not in known_targets]
    if unknown_targets:
        raise ContractViolationError(
            f"unknown targets {unknown_targets}; known targets: "
            f"{list(known_targets)}",
            code="unknown_target",
        )
    stray_fail = sorted(failing_targets - set(targets))
    if stray_fail:
        raise ContractViolationError(
            f"failing_targets {stray_fail} are not among the requested "
            f"targets {list(targets)}",
            code="unknown_fail_target",
        )

    if not stages:
        raise ContractViolationError(
            "no rollout stages requested; refusing to run an empty rollout",
            code="empty_stages",
        )
    unknown_stages = [s for s in stages if s not in STAGE_CHAIN]
    if unknown_stages:
        raise ContractViolationError(
            f"unknown stages {unknown_stages}; legal stages: "
            f"{list(STAGE_CHAIN)}",
            code="unknown_stage",
        )
    legal_prefixes = [STAGE_CHAIN[: i + 1] for i in range(len(STAGE_CHAIN))]
    if tuple(stages) not in legal_prefixes:
        raise ContractViolationError(
            f"stage sequence {list(stages)} is not a legal monotonic "
            f"progression; expected a contiguous prefix of "
            f"{list(STAGE_CHAIN)} (no reordering, gaps or duplicates)",
            code="invalid_stage_order",
        )

    return ValidatedRequest(
        targets=tuple(targets),
        stages=tuple(stages),
        failing_targets=frozenset(failing_targets),
    )
