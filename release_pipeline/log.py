"""Structured-but-human-readable diagnostics on stderr.

Keeping diagnostics on stderr leaves stdout reserved for machine-readable
CLI output (JSON), while tests still capture the key inputs and the basis
for each verdict.
"""

from __future__ import annotations

import sys


def log(component: str, message: str, **fields: object) -> None:
    suffix = ""
    if fields:
        suffix = " " + " ".join(f"{k}={v}" for k, v in sorted(fields.items()))
    print(f"[{component}] {message}{suffix}", file=sys.stderr, flush=True)
