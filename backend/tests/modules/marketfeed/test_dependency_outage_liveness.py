"""A venue or database outage is a readiness fact, never a restart.

Before the liveness/readiness split, the ``ws`` liveness beat was recorded only
when Bitfinex delivered a frame and ``db_keepalive`` only when ``SELECT 1``
succeeded, so a Bitfinex outage past 270s or a database outage past 21 min made
``scan_staleness`` raise ``FatalError`` and ``/healthz`` answer 503, and the
process restarted into the same outage. Now the liveness beats mark the tasks'
own loop iterations; the dependency answers feed ``ws_data`` / ``db``, which
flip ``/readyz`` while the per-submit gates keep submitting blocked.

Each test seeds the heartbeats as they stood when the outage began (last
frame / last successful ping long ago), runs one iteration of the real task
code, then asserts liveness, readiness and the submit gate. Ages are minutes
away from every threshold, so no timing assumption decides the outcome.

Mutation checks (one at a time; revert after each):

* record ``ws`` only inside the "frame is fresh" branch of
  ``Daemon._ws_freshness_tick``: ``test_a_bitfinex_ws_outage_*`` raises FatalError.
* move ``on_attempt()`` into the success branch of ``keepalive_loop``:
  ``test_a_database_outage_*`` raises FatalError.
* drop the dependency branch in ``scan_staleness``: readiness stays ready.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core import keepalive
from bfx_funding_bot.core.health import (
    DB_FRESHNESS,
    LIVENESS_THRESHOLDS,
    MARKET_DATA_FRESHNESS,
    HealthProbe,
)
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.audit.recorder import ExecutionDecisionRecorder
from bfx_funding_bot.modules.execution.contracts import (
    BlockedExecution,
    BlockReason,
    ExecutionPolicy,
    GuardResult,
)
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.safety.hard_guards import HeartbeatGuard
from bfx_funding_bot.modules.marketfeed.daemon import Daemon
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthMonitor
from bfx_funding_bot.modules.marketfeed.healthz import make_app
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness
from tests.modules.execution.deployment.test_eligibility import (
    _candidate,
    _context,
    _price,
    _snapshot,
)

# Live HeartbeatGuard threshold (configs/safety.live.yaml).
_LIVE_GUARD_THRESHOLD_S = 300


class _Sink:
    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emitted.append(event)


class _SilentWS:
    """A public WS client that has not seen a frame for ten minutes."""

    def last_msg_age_ms(self) -> int:
        return 10 * 60 * 1000

    def reconnect_count_last_hour(self) -> int:
        return 3


class _FreshWS(_SilentWS):
    def last_msg_age_ms(self) -> int:
        return 1_000


def _monitor(probe: HealthProbe, readiness: TradingReadiness) -> HealthMonitor:
    return HealthMonitor(phase=Phase.LIVE, event_sink=_Sink(), probe=probe, readiness=readiness)


def _fresh_liveness(probe: HealthProbe) -> None:
    now = datetime.now(UTC)
    for task in LIVENESS_THRESHOLDS:
        probe.last_active_ts[task] = now


async def _gate(safety_allowed: bool, guard_reason: str | None, *, audit: Any,
                readiness: TradingReadiness) -> object:
    candidate = _candidate()
    gate = ExecutionGate(policy=ExecutionPolicy.BOOK_GUARDED, audit=audit, readiness=readiness)
    return await gate.prepare(
        candidate,
        decision_id="decision-outage",
        reconcile_id="reconcile-outage",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=None,
        safety=GuardResult(safety_allowed, "heartbeat", guard_reason),
        audit_context=_context(candidate),
    )


class _AcceptingAudit:
    async def record(self, decision: object) -> None:
        return None


async def test_a_bitfinex_ws_outage_keeps_liveness_and_blocks_trading_through_readiness() -> None:
    probe = HealthProbe()
    _fresh_liveness(probe)
    outage_began = datetime.now(UTC) - timedelta(minutes=10)
    # Before the outage both beats came from the same fresh frame.
    probe.last_active_ts["ws"] = outage_began
    probe.last_active_ts[MARKET_DATA_FRESHNESS] = outage_began
    readiness = TradingReadiness()
    readiness.set_ready()

    Daemon._ws_freshness_tick(SimpleNamespace(probe=probe, ws_client=_SilentWS()))  # type: ignore[arg-type]

    stale = await _monitor(probe, readiness).scan_staleness()  # must not raise FatalError
    assert [record["sub_task"] for record in stale] == [MARKET_DATA_FRESHNESS]
    client = TestClient(make_app(probe, readiness=readiness))
    assert client.get("/healthz").status_code == 200
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json() == {"trading_ready": False, "reason": "dependency_stale"}

    guard = HeartbeatGuard(probe=probe, threshold_seconds=_LIVE_GUARD_THRESHOLD_S,
                           watched_sub_tasks=[MARKET_DATA_FRESHNESS])
    verdict = await guard.evaluate(_candidate(), None)  # type: ignore[arg-type]
    assert verdict.allowed is False
    result = await _gate(verdict.allowed, verdict.reason, audit=_AcceptingAudit(),
                         readiness=readiness)
    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.SAFETY_GUARD_BLOCKED

    # Frames again: the dependency clears on the next scan, without a restart;
    # readiness falls back to the last decision (blocked above) until the next one.
    Daemon._ws_freshness_tick(SimpleNamespace(probe=probe, ws_client=_FreshWS()))  # type: ignore[arg-type]
    await _monitor(probe, readiness).scan_staleness()
    assert readiness.snapshot().reason == BlockReason.SAFETY_GUARD_BLOCKED.value
    assert (await guard.evaluate(_candidate(), None)).allowed is True  # type: ignore[arg-type]


class _UnreachableEngine:
    """Refuses the connection; the first attempt also ends the keepalive loop."""

    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop

    def connect(self) -> object:
        self._stop.set()
        raise ConnectionRefusedError("database is down")


async def test_a_database_outage_keeps_liveness_and_blocks_trading_through_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = HealthProbe()
    _fresh_liveness(probe)
    outage_began = datetime.now(UTC) - timedelta(hours=1)
    # Before the outage both beats came from the same successful ping.
    probe.last_active_ts["db_keepalive"] = outage_began
    probe.last_active_ts[DB_FRESHNESS] = outage_began
    readiness = TradingReadiness()
    readiness.set_ready()
    stop = asyncio.Event()
    original = keepalive.keepalive_loop

    async def without_the_interval(engine: Any, **kwargs: Any) -> None:
        await original(engine, interval_s=0.0, **kwargs)

    monkeypatch.setattr(keepalive, "keepalive_loop", without_the_interval)
    fake = SimpleNamespace(probe=probe, db_engine=_UnreachableEngine(stop), _stop_event=stop)
    await Daemon._db_keepalive_loop(fake)  # type: ignore[arg-type]

    stale = await _monitor(probe, readiness).scan_staleness()  # must not raise FatalError
    assert [record["sub_task"] for record in stale] == [DB_FRESHNESS]
    client = TestClient(make_app(probe, readiness=readiness))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").json() == {"trading_ready": False, "reason": "dependency_stale"}

    # The submit gate: a READY decision needs its audit row committed first
    # (eligibility.ExecutionGate), and the database does not answer.
    down = create_async_engine("postgresql+asyncpg://bfx:bfx@127.0.0.1:1/bfx")
    try:
        audit = ExecutionDecisionRecorder(async_sessionmaker(down, expire_on_commit=False))
        result = await _gate(True, None, audit=audit, readiness=readiness)
    finally:
        await down.dispose()
    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.EXECUTION_AUDIT_UNAVAILABLE
