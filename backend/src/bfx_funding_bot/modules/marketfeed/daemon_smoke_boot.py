"""Boot-time smoke L2 wrapper — called from daemon._run() between
build_daemon() and TaskGroup start.

Extracted for testability: _run() itself touches signal handlers + TaskGroup
and is hard to unit-test in isolation.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from bfx_funding_bot.core.telemetry import EventType, HealthStatus, Level

log = logging.getLogger(__name__)

BOOT_SMOKE_TIMEOUT_S = 30.0


async def run_boot_smoke(daemon: Any) -> None:
    """Run boot smoke L2. Failure → log critical + emit SAFETY_TRIGGER.

    Daemon unconditionally continues — smoke crash never crashes daemon.
    `daemon` is typed loosely (Daemon | Mock) to keep this helper unit-testable.
    """
    if daemon.smoke_runner is None:
        log.info("smoke_boot_skipped reason=no_smoke_runner")
        return
    try:
        result = await asyncio.wait_for(
            daemon.smoke_runner.run_l2(), timeout=BOOT_SMOKE_TIMEOUT_S,
        )
        if result.status == "pass":
            log.info("smoke_boot_passed duration_ms=%d", result.duration_ms)
            return
        raise RuntimeError(
            f"smoke_boot_l2_fail: {result.error!r} checks={result.checks}",
        )
    except Exception as exc:
        log.critical("smoke_boot_failed err=%r — daemon continues", exc)
        try:
            # NOTE: boot-smoke failure is not a guard block, so this SAFETY_TRIGGER
            # payload intentionally differs from SafetyTriggerPayload (no guard_name/
            # reason/decision_snapshot). diagnostics rows with kind=safety_trigger are
            # therefore heterogeneous — query defensively. (3b follow-up: consider a
            # dedicated kind if this grows.)
            await daemon.diagnostics.emit({
                "timestamp": datetime.now(UTC).isoformat(),
                "level": Level.CRITICAL.value,
                "phase": daemon.config.phase.value,
                "strategy": None,
                "cell": None,
                "event_type": EventType.SAFETY_TRIGGER.value,
                "correlation_id": str(uuid4()),
                "payload": {
                    "check_target": "smoke_boot",
                    "status": HealthStatus.DEGRADED.value,
                    "error_message": repr(exc),
                },
            })
        except Exception:
            log.exception("smoke_boot_safety_trigger_emit_failed")
