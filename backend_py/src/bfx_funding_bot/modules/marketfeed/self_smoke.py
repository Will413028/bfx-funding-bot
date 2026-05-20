"""G1 self-smoke trigger helper.

Spec: docs/superpowers/specs/2026-05-21-g1-c1-continuity-redesign-design.md
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

SLEEP_BEFORE_SMOKE_SECONDS = 30


async def maybe_run_self_smoke(
    *,
    phase: str,
    duration: int | None,
    cells: list[dict[str, Any]],
    smoke_runner: Callable[..., Awaitable[int]],
    sleep_fn: Callable[[float], Awaitable[None]],
) -> int:
    """Run G1 smoke checks if gating conditions hold; otherwise return 0.

    Gated by: phase == "paper" AND duration is not None.

    Exception case is NOT a gate — daemon._run() propagates exceptions via
    `except* Exception: raise` so this helper is unreachable on daemon failure.
    SIGTERM (CancelledError) falls through to here intentionally — smoke runs
    against whatever was emitted before stop.

    Returns the smoke exit code (0 if skipped because not gated).
    """
    if phase != "paper" or duration is None:
        return 0

    await sleep_fn(SLEEP_BEFORE_SMOKE_SECONDS)
    return await smoke_runner(phase=phase, hours=duration, cells=cells)
