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

# Per spec D4 heartbeat threshold table — staleness threshold in seconds.
SUB_TASK_THRESHOLDS: dict[str, int] = {
    "ws": 90,                    # Phase 4.2.0 lesson v2: poll ws_client.last_msg_age_ms()
                                 # every 15s; threshold 90s covers 4-5 missed Bitfinex
                                 # `hb` frames (which arrive ~15s on subscribed channels).
    "candle_writer": 65 * 60,    # Phase 4.2.0 d90363fa lesson v1: 1h funding cells
                                 # publish candle only on tick — can be silent
                                 # >5min in quiet markets. ws heartbeat poller is
                                 # the fast-zombie detector now; candle_writer
                                 # only catches truly stuck queues (>3hr).
    "scheduler": 65 * 60,        # hourly boundary + buffer
    "axiom": 60,                 # trading event cadence + 5min hb
    "health_check": 6 * 60,      # 5min hb + buffer
    "db_keepalive": 7 * 60,      # 5min interval + 2min buffer
    # Phase 4.2 Task 19: execution-pipeline sub-tasks.
    "safety_chain": 6 * 60,      # 5min watchdog + 1min buffer
    "executor": 6 * 60,          # 5min watchdog + 1min buffer
    "fill_tracker": 90,          # 30s poll cadence x 3 missed
}
_DEFAULT_THRESHOLD_S = 60


class _AxiomProtocol(Protocol):
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
        axiom: _AxiomProtocol,
        probe: HealthProbe,
        heartbeat_interval_s: float = 300.0,
    ) -> None:
        self.phase = phase
        self.axiom = axiom
        self.probe = probe
        self.heartbeat_interval_s = heartbeat_interval_s
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

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
        await self.axiom.emit({
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
        any stale. Raises FatalError on 3× threshold breach.

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
            if age_s <= threshold:
                continue

            severity = "down" if age_s > 2 * threshold else "degraded"
            stale.append({
                "sub_task": sub_task,
                "severity": severity,
                "age_s": age_s,
            })

            # Emit BEFORE potential fatal escalation so axiom sees root cause
            level = Level.ERROR if severity == "down" else Level.WARN
            status = HealthStatus.DOWN if severity == "down" else HealthStatus.DEGRADED
            await self.axiom.emit({
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

            # Escalate to fatal AFTER emit, so axiom sees the root cause
            if age_s > 3 * threshold:
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
                await self.axiom.emit({
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
