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

from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
    Phase,
)

log = logging.getLogger(__name__)


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
    """In-process pub-sub for per-target state. Updates from WS / db / redis
    callers; HealthMonitor consumes and emits to Axiom."""

    def __init__(self) -> None:
        self._state: dict[HealthTarget, _TargetState] = {}

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
