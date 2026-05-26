"""Health monitor — state-change emit + 5min heartbeat.

設計依據: phase4.1-paper-shadow-infra-design.md Section "Data Flow" Flow 4
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from bfx_funding_bot.core.errors import FatalError
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
    Phase,
)

log = logging.getLogger(__name__)

# ── Liveness sub-tasks (own-loop, event-loop-driven) ──────────────────────────
# A stale heartbeat means the loop is stuck or the event loop is deadlocked →
# restarting the process can recover. These DRIVE /healthz 503 (Koyeb restart)
# and scan_staleness FatalError. Per spec D4 heartbeat threshold table.
LIVENESS_THRESHOLDS: dict[str, int] = {
    "ws": 90,                    # Phase 4.2.0 lesson v2: poll ws_client.last_msg_age_ms()
                                 # every 15s; threshold 90s covers 4-5 missed Bitfinex
                                 # `hb` frames (which arrive ~15s on subscribed channels).
    "candle_writer": 65 * 60,    # Phase 4.2.0 d90363fa lesson v1: 1h funding cells
                                 # publish candle only on tick — can be silent
                                 # >5min in quiet markets. ws heartbeat poller is
                                 # the fast-zombie detector now; candle_writer
                                 # only catches truly stuck queues (>3hr).
    "scheduler": 65 * 60,        # hourly boundary + buffer
    "health_check": 6 * 60,      # 5min hb + buffer
    "db_keepalive": 7 * 60,      # 5min interval + 2min buffer
    "fill_tracker": 90,          # 30s poll cadence x 3 missed
}

# ── Activity sub-tasks (reactive middleware) ──────────────────────────────────
# executor/safety_chain are bumped ONLY when a POST decision flows through the
# chain (execution/middleware/heartbeat.py, signal_engine.py:290). A stale
# heartbeat means "no trading activity", NOT a failure. Tying these to liveness
# caused the 2026-05-26 canary restart loop (idle market → stale → /healthz 503
# → restart) — a textbook k8s anti-pattern (liveness must not depend on business
# activity). These are emitted as WARN for observability but NEVER drive a
# restart, FatalError, or trade block.
ACTIVITY_THRESHOLDS: dict[str, int] = {
    "safety_chain": 6 * 60,
    "executor": 6 * 60,
}

# Merged view: scan_staleness needs a threshold for both classes to emit. The
# liveness/fatal gating is keyed on LIVENESS_THRESHOLDS membership, not on this.
SUB_TASK_THRESHOLDS: dict[str, int] = {**LIVENESS_THRESHOLDS, **ACTIVITY_THRESHOLDS}
_DEFAULT_THRESHOLD_S = 60

# A sub-task is EITHER liveness OR activity, never both. Overlap would make the
# merged dict silently take the ACTIVITY value and the scan_staleness gate would
# suppress fatal escalation for a task meant to be liveness — enforce at import.
assert not (LIVENESS_THRESHOLDS.keys() & ACTIVITY_THRESHOLDS.keys()), (
    "sub-task threshold keys must not overlap between LIVENESS and ACTIVITY"
)


class _EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


@dataclass
class _TargetState:
    status: HealthStatus = HealthStatus.HEALTHY
    last_msg_age_ms: int | None = None
    reconnect_count_last_hour: int | None = None
    latency_ms: int | None = None
    error_message: str | None = None
    dirty: bool = False


class HealthProbe:
    """In-process pub-sub for per-target state + heartbeat registry.

    Existing API: update() for HealthTarget state (WS / DB / Redis status).
    Added (D4): record_heartbeat() for sub-task progress timestamps and
    last_active_ts dict keyed by sub-task name.
    Added (Phase 4.3): cell_pipeline_status registry for per-cell
    SIGNAL_PIPELINE state — readable by scan_staleness carve-out (Task 5).
    """

    def __init__(self) -> None:
        self._state: dict[HealthTarget, _TargetState] = {}
        self.last_active_ts: dict[str, datetime] = {}
        # Per-cell SIGNAL_PIPELINE health state for sticky transition logic.
        # Keyed by pair_id (strategy:cell_id) so two strategies on the same
        # cell are independent state machines.
        self.cell_pipeline_status: dict[str, HealthStatus] = {}

    def get_cell_pipeline_status(self, pair_id: str) -> HealthStatus | None:
        """Return current SIGNAL_PIPELINE status for a cell x strategy pair.

        Used by scan_staleness carve-out (Task 5) — SIGNAL_PIPELINE DEGRADED
        with reason=stale_exceeded never escalates to fatal. Keyed by pair_id
        (strategy:cell_id format) since two strategies on same cell are
        independent state machines.
        """
        return self.cell_pipeline_status.get(pair_id)

    def set_cell_pipeline_status(self, pair_id: str, status: HealthStatus) -> None:
        """Update per-cell pipeline status. Called by daemon LOCF emit path."""
        self.cell_pipeline_status[pair_id] = status

    def record_heartbeat(self, sub_task: str) -> None:
        """Sub-task calls this each time it completes a progress unit."""
        self.last_active_ts[sub_task] = datetime.now(UTC)

    def current_status(self, target: HealthTarget) -> HealthStatus | None:
        """Return last-set status for a target, or None if never set.

        Used by daemon code to detect transitions (avoid update spam when
        target stays HEALTHY across many polling ticks).
        """
        state = self._state.get(target)
        return state.status if state is not None else None

    def update(
        self, target: HealthTarget, status: HealthStatus, **fields: Any,
    ) -> None:
        cur = self._state.setdefault(target, _TargetState())
        changed = cur.status != status or any(
            getattr(cur, k, None) != v for k, v in fields.items()
        )
        cur.status = status
        for k, v in fields.items():
            setattr(cur, k, v)
        cur.dirty = changed

    def snapshot(self) -> dict[HealthTarget, _TargetState]:
        return self._state

    def drain_dirty(self) -> dict[HealthTarget, _TargetState]:
        out = {t: s for t, s in self._state.items() if s.dirty}
        for s in out.values():
            s.dirty = False
        return out


class HealthMonitor:
    def __init__(
        self,
        *,
        phase: Phase,
        event_sink: _EventSink,
        probe: HealthProbe,
        heartbeat_interval_s: float = 300.0,
    ) -> None:
        self.phase = phase
        self._events = event_sink
        self.probe = probe
        self.heartbeat_interval_s = heartbeat_interval_s
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Last-emitted staleness severity per activity-class sub-task, for
        # transition-only emit (see scan_staleness). Cleared on recovery so a
        # later re-staleness re-emits the healthy→degraded transition.
        self._last_activity_severity: dict[str, str] = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _loop(self) -> None:
        last_beat = 0.0
        while not self._stop.is_set():
            for target, state in self.probe.drain_dirty().items():
                await self._emit(target, state)
            loop_time = asyncio.get_running_loop().time()
            if loop_time - last_beat >= self.heartbeat_interval_s:
                last_beat = loop_time
                for target, state in self.probe.snapshot().items():
                    await self._emit(target, state)
            await asyncio.sleep(0.05)

    async def _emit(self, target: HealthTarget, state: _TargetState) -> None:
        if state.status == HealthStatus.HEALTHY:
            level = Level.INFO
        elif state.status == HealthStatus.DEGRADED:
            level = Level.WARN
        else:
            level = Level.ERROR
        payload: dict[str, Any] = {
            "check_target": target.value,
            "status": state.status.value,
        }
        if state.last_msg_age_ms is not None:
            payload["last_msg_age_ms"] = state.last_msg_age_ms
        if state.reconnect_count_last_hour is not None:
            payload["reconnect_count_last_hour"] = state.reconnect_count_last_hour
        if state.latency_ms is not None:
            payload["latency_ms"] = state.latency_ms
        if state.error_message:
            payload["error_message"] = state.error_message
        await self._events.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level.value,
            "phase": self.phase.value,
            "strategy": None, "cell": None,
            "event_type": EventType.HEALTH_CHECK.value,
            "correlation_id": str(uuid4()),
            "payload": payload,
        })

    async def scan_staleness(self) -> list[dict[str, Any]]:
        """Check last_active_ts for each sub-task. Emit + return records for
        any stale. Raises FatalError on 3× threshold breach for liveness
        sub-tasks; activity-class sub-tasks (executor, safety_chain) emit but
        never escalate.

        SIGNAL_PIPELINE carve-out (Phase 4.3 Task 5):
        Per-cell pipeline state is tracked separately in
        probe.cell_pipeline_status (keyed by pair_id). These cells represent
        expected sparseness (Bitfinex p30 channel physics), NOT system failure
        — reason=stale_exceeded NEVER escalates to FatalError. The carve-out
        is structural: SIGNAL_PIPELINE is never registered in last_active_ts,
        so the fatal escalation path physically cannot touch it. Instead, a
        separate read-only pass emits observability events for degraded cells
        but always returns without raising.

        Returns: list of {sub_task, severity, age_s} for stale tasks (for
                 inspection / test). Tasks under threshold are not included.
                 SIGNAL_PIPELINE cells appear as
                 {sub_task: "SIGNAL_PIPELINE:<pair_id>", severity: "degraded"|"down",
                  age_s: None} — never with severity "fatal".
        """
        now = datetime.now(UTC)
        stale: list[dict[str, Any]] = []

        # ── Existing per-sub-task heartbeat scan ──────────────────────────────
        # FatalError CAN be raised here for connection_lost / db_unavailable /
        # task_hung reasons. SIGNAL_PIPELINE keys are never inserted into
        # last_active_ts so they are physically excluded from this path.
        for sub_task, last_ts in self.probe.last_active_ts.items():
            threshold = SUB_TASK_THRESHOLDS.get(sub_task, _DEFAULT_THRESHOLD_S)
            age_s = (now - last_ts).total_seconds()
            is_activity = sub_task in ACTIVITY_THRESHOLDS
            if age_s <= threshold:
                # Recovered (or never stale): reset transition state so a later
                # re-staleness re-emits the healthy→degraded transition.
                if is_activity:
                    self._last_activity_severity.pop(sub_task, None)
                continue

            severity = "down" if age_s > 2 * threshold else "degraded"
            stale.append({
                "sub_task": sub_task,
                "severity": severity,
                "age_s": age_s,
            })

            # Activity-class (reactive executor/safety_chain) sub-tasks emit only
            # on a severity TRANSITION. A persistently stale executor in an idle
            # market would otherwise spam one WARN per scan (~30s) — pure noise,
            # since these never escalate to FatalError. Liveness sub-tasks still
            # emit every scan: they escalate at 3× threshold (restart), so the
            # repeated emits are short-lived and show the climbing age.
            if is_activity:
                if self._last_activity_severity.get(sub_task) == severity:
                    continue
                self._last_activity_severity[sub_task] = severity

            # Emit BEFORE potential fatal escalation so the event is persisted before raise
            level = Level.ERROR if severity == "down" else Level.WARN
            status = HealthStatus.DOWN if severity == "down" else HealthStatus.DEGRADED
            await self._events.emit({
                "timestamp": now.isoformat(),
                "level": level.value,
                "phase": self.phase.value,
                "strategy": None, "cell": None,
                "event_type": EventType.HEALTH_CHECK.value,
                "correlation_id": str(uuid4()),
                "payload": {
                    "check_target": sub_task,
                    "status": status.value,
                    "last_msg_age_ms": int(age_s * 1000),
                    "error_message": f"heartbeat stale {age_s:.0f}s > {threshold}s threshold",
                },
            })

            # Escalate to fatal AFTER emit, so the event is persisted before raise.
            # Activity-class sub-tasks (reactive: executor / safety_chain) never
            # escalate — a stale executor means "no trades flowed", not a stuck
            # process. Liveness sub-tasks and unknown keys (default threshold)
            # still escalate so a genuinely hung own-loop task triggers restart.
            if age_s > 3 * threshold and sub_task not in ACTIVITY_THRESHOLDS:
                raise FatalError(
                    f"sub_task={sub_task} stale {age_s:.0f}s > "
                    f"3× threshold ({3 * threshold}s) — escalating fatal"
                )

        # ── SIGNAL_PIPELINE per-cell scan (carve-out: NEVER raises FatalError) ─
        # Reads cell_pipeline_status populated by daemon LOCF emit path (Task 4).
        # reason=stale_exceeded is expected sparseness — spec explicitly prohibits
        # fatal escalation regardless of how long the cell has been DEGRADED.
        # We emit a WARN for observability but always continue without escalating.
        for pair_id, cell_status in self.probe.cell_pipeline_status.items():
            if cell_status in (HealthStatus.DEGRADED, HealthStatus.DOWN):
                severity = cell_status.value  # "degraded" or "down"
                stale.append({
                    "sub_task": f"SIGNAL_PIPELINE:{pair_id}",
                    "severity": severity,
                    "age_s": None,  # wall-clock age not tracked here; daemon owns TTL
                })
                await self._events.emit({
                    "timestamp": now.isoformat(),
                    "level": Level.WARN.value,
                    "phase": self.phase.value,
                    "strategy": None, "cell": pair_id,
                    "event_type": EventType.HEALTH_CHECK.value,
                    "correlation_id": str(uuid4()),
                    "payload": {
                        "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                        "status": severity,
                        "error_message": (
                            f"pair_id={pair_id} pipeline {cell_status.value.upper()} "
                            "(stale_exceeded — expected sparseness, not escalating)"
                        ),
                    },
                })
                # NOTE: no FatalError here — spec carve-out for stale_exceeded

        return stale
