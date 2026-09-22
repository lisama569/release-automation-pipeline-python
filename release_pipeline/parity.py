"""Parity between local and CI execution paths.

The pipeline must reach the *same conclusion* in both places. This module
makes the execution contract explicit instead of relying on ambient
environment defaults:

* the effective environment fingerprint is computed from an explicit
  allow-list (or a caller-supplied mapping) — never from whatever happens to
  be exported in a shell;
* unsupported CLI/configuration parameters are rejected in ``ci`` mode and
  recorded as an explained degradation in ``local`` mode (never silently
  ignored);
* optional capabilities (e.g. an advisory-lock backend) are probed; a
  missing capability degrades only when a safe local fallback exists, and
  the degradation is returned in the report.
"""

from __future__ import annotations

import platform
import sys

from .errors import ParityError
from .log import log

SUPPORTED_MODES = frozenset({"local", "ci"})

# Parameters both paths understand. Anything else is drift by definition.
SUPPORTED_PARAMS = frozenset(
    {
        "targets",
        "failing_targets",
        "declared_version",
        "env",
        "config",
        "stages",
        "mode",
        "work_dir",
        "health_policy",
    }
)


def fingerprint_runtime() -> dict[str, str]:
    """Stable runtime fingerprint (independent of host env vars)."""
    return {
        "python": ".".join(str(p) for p in sys.version_info[:3]),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def probe_capabilities() -> dict[str, bool]:
    """Report whether optional safety capabilities are available."""
    caps = {"fcntl_locks": True}
    try:
        import fcntl  # noqa: F401
    except ImportError:  # pragma: no cover - non-posix
        caps["fcntl_locks"] = False
    return caps


def check_parity(
    *,
    mode: str,
    env: dict[str, str] | None = None,
    supported_params: frozenset[str] | None = None,
    given_params: frozenset[str] | None = None,
) -> dict[str, str]:
    """Validate mode/params and return a parity report.

    Returns a dict containing the effective ``mode``, the ``env_fp`` basis
    (sorted mapping) and any ``degraded`` reasons. Raises
    :class:`ParityError` when drift would change the verdict.
    """
    if mode not in SUPPORTED_MODES:
        raise ParityError(
            f"unsupported execution mode {mode!r}; expected one of "
            f"{sorted(SUPPORTED_MODES)}",
            code="unsupported_mode",
        )

    supported = supported_params or SUPPORTED_PARAMS
    given = given_params or frozenset()
    unsupported = sorted(given - supported)
    degraded: list[str] = []

    if unsupported:
        if mode == "ci":
            # CI must never silently accept parameters it cannot honor.
            raise ParityError(
                f"unsupported parameters rejected in ci mode: {unsupported}",
                code="unsupported_param",
            )
        degraded.append(
            "ignored-params(explicitly degraded in local mode): "
            + ",".join(unsupported)
        )

    caps = probe_capabilities()
    if not caps["fcntl_locks"]:
        if mode == "ci":
            raise ParityError(
                "advisory file locks unavailable; ci run cannot guarantee "
                "concurrency safety",
                code="capability_missing",
            )
        degraded.append("fcntl locks unavailable; using local fallback locking")

    effective_env = dict(sorted((env or {}).items()))
    report = {
        "mode": mode,
        "runtime": ";".join(f"{k}={v}" for k, v in sorted(
            fingerprint_runtime().items())),
        "env_basis": ";".join(f"{k}={v}" for k, v in effective_env.items()),
        "degraded": " | ".join(degraded),
    }
    log(
        "parity",
        "check",
        mode=mode,
        unsupported=unsupported or "-",
        degraded=report["degraded"] or "-",
    )
    return report
