"""Request-contract validation shared by real runs and dry-run plans.

Every check raises :class:`InvalidRequestError` with a machine-distinguishable
``code`` *before* any directory is created or any state is touched, so an
invalid request never executes partially and never leaves traces.

Contract
--------
* ``empty_targets``            – the requested target set is empty
* ``duplicate_target``         – the same target name appears twice
* ``unknown_target``           – ``failing_targets`` names a target that was
                                 not requested
* ``empty_stages``             – the requested stage sequence is empty
* ``unknown_stage``            – a stage name (in the sequence or in the
                                 health policy) is not a known rollout stage
* ``invalid_stage_sequence``   – the stage sequence is not a strictly
                                 increasing subsequence of
                                 ``staging -> canary -> full``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import InvalidRequestError

if TYPE_CHECKING:  # avoid a runtime import cycle with pipeline.py
    from .pipeline import PipelineConfig

# Canonical rollout order; a requested stage sequence must be a strictly
# increasing subsequence of this chain.
STAGE_ORDER: tuple[str, ...] = ("staging", "canary", "full")
_KNOWN_STAGES = frozenset(STAGE_ORDER)


def validate_config(cfg: "PipelineConfig") -> None:
    """Reject contract-violating requests before any side effect.

    Raises :class:`InvalidRequestError` with a specific ``code``; never
    returns normally for an invalid request and never mutates anything.
    """
    targets = list(cfg.targets)
    if not targets:
        raise InvalidRequestError(
            "requested target set is empty; nothing to build or deploy",
            code="empty_targets",
        )
    seen: set[str] = set()
    for name in targets:
        if name in seen:
            raise InvalidRequestError(
                f"duplicate target {name!r} in request",
                code="duplicate_target",
            )
        seen.add(name)

    unknown_targets = sorted(set(cfg.failing_targets) - set(targets))
    if unknown_targets:
        raise InvalidRequestError(
            f"failing_targets reference unknown targets: {unknown_targets}",
            code="unknown_target",
        )

    stages = list(cfg.stages)
    if not stages:
        raise InvalidRequestError(
            "requested stage sequence is empty; a rollout must declare at "
            "least one stage",
            code="empty_stages",
        )
    for stage in stages:
        if stage not in _KNOWN_STAGES:
            raise InvalidRequestError(
                f"unknown stage {stage!r}; known stages: {list(STAGE_ORDER)}",
                code="unknown_stage",
            )
    ranks = [STAGE_ORDER.index(s) for s in stages]
    if ranks != sorted(ranks) or len(set(ranks)) != len(ranks):
        raise InvalidRequestError(
            f"stage sequence {stages} is not a strictly increasing "
            f"subsequence of {list(STAGE_ORDER)}",
            code="invalid_stage_sequence",
        )

    unknown_health = sorted(set(cfg.health_policy) - _KNOWN_STAGES)
    if unknown_health:
        raise InvalidRequestError(
            f"health policy references unknown stages: {unknown_health}",
            code="unknown_stage",
        )
