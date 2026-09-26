"""Pure rollout planning shared by preview and real execution.

Everything here is a pure function of ``(stage chain, current stage, health
policy)``: no I/O, no clock, no randomness. Preview and the real pipeline
both consume these plans, which is what guarantees their conclusions agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import RolloutStage


@dataclass(frozen=True)
class StageStep:
    """One planned stage transition."""

    from_stage: str
    to_stage: str
    advances: bool
    blocked_reason: str


@dataclass(frozen=True)
class RolloutPlan:
    """The full planned trajectory for one target."""

    steps: tuple[StageStep, ...]
    final_stage: str
    blocked: bool
    blocked_at: str


def health_verdict_for(
    stage: RolloutStage,
    health_policy: dict[str, tuple[bool, bool]],
    *,
    visible: bool = True,
) -> tuple[bool, bool, str]:
    """Pure health verdict: (healthy, conclusive, detail).

    Mirrors :meth:`SimulatedEnvironment.check_health` exactly, but as a pure
    function so preview and execution can never drift apart.
    """
    if not visible:
        return (False, True, "no release visible")
    healthy, conclusive = tuple(health_policy.get(stage.value, (True, True)))
    if not conclusive:
        detail = f"health inconclusive at stage {stage.value}"
    elif healthy:
        detail = f"health checks passed at stage {stage.value}"
    else:
        detail = f"health checks FAILED at stage {stage.value}"
    return (healthy, conclusive, detail)


def plan_rollout(
    current_stage: RolloutStage,
    stages: tuple[str, ...],
    health_policy: dict[str, tuple[bool, bool]],
) -> RolloutPlan:
    """Plan advancement from ``current_stage`` through ``stages``.

    Replicates the real pipeline loop: advance one stage at a time while the
    current stage is inside the configured chain and the chain's end has not
    been reached; stop at the first non-advancing verdict (blocked).
    """
    chain = [RolloutStage.NONE] + [RolloutStage(s) for s in stages]
    steps: list[StageStep] = []
    current = current_stage
    blocked = False
    blocked_at = ""
    while current in chain and current != chain[-1]:
        nxt = chain[chain.index(current) + 1]
        healthy, conclusive, detail = health_verdict_for(
            nxt, health_policy, visible=True
        )
        if healthy and conclusive:
            steps.append(
                StageStep(
                    from_stage=current.value,
                    to_stage=nxt.value,
                    advances=True,
                    blocked_reason="",
                )
            )
            current = nxt
            continue
        reason_code = (
            "health-inconclusive" if not conclusive else "health-failed"
        )
        steps.append(
            StageStep(
                from_stage=current.value,
                to_stage=nxt.value,
                advances=False,
                blocked_reason=f"{reason_code}: {detail}",
            )
        )
        blocked = True
        blocked_at = nxt.value
        break
    return RolloutPlan(
        steps=tuple(steps),
        final_stage=current.value,
        blocked=blocked,
        blocked_at=blocked_at,
    )
