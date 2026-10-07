"""A venue or database outage is a readiness fact, never a restart; and a
dependency never seen since boot keeps trading blocked until its first answer.

Before the liveness/readiness split, the ``ws`` liveness beat was recorded only
when Bitfinex delivered a frame and ``db_keepalive`` only when ``SELECT 1``
succeeded, so a Bitfinex outage past 270s or a database outage past 21 min made
``scan_staleness`` raise ``FatalError`` and ``/healthz`` answer 503, and the
process restarted into the same outage. Now the liveness beats mark the tasks'
own loop iterations; the dependency answers feed ``ws_data`` / ``db``, which
flip ``/readyz`` while the per-submit gates keep submitting blocked, and the
next answer clears ``/readyz`` at once.

Each outage test seeds the heartbeats as they stood when the outage began (last
frame / last successful ping long ago), runs one iteration of the real task
code, then asserts liveness, readiness and the submit gate. Ages are minutes
away from every threshold, so no timing assumption decides the outcome.

Mutation checks (one at a time; revert after each):

* record ``ws`` only inside the "frame is fresh" branch of
  ``Daemon._ws_freshness_tick``: ``test_a_bitfinex_ws_outage_*`` raises FatalError.
* move ``on_attempt()`` into the success branch of ``keepalive_loop``:
  ``test_a_database_outage_*`` raises FatalError.
* drop the dependency branch in ``scan_staleness``: readiness stays ready.
* let HeartbeatGuard pass a never-recorded key, or take ``ws_data`` from
  ``last_msg_age_ms``: ``test_nothing_trades_before_*``.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
from tests.modules.execution.deployment.helpers import (
    make_audit_context,
    make_candidate,
    make_price,
    make_snapshot,
)


class _Sink:
    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emitted.append(event)


class _WS:
    """A public WS client whose newest received frame is ``frame_age_ms`` old
    (None: this client has received none yet)."""

    def __init__(self, frame_age_ms: int | None) -> None:
        self.frame_age_ms = frame_age_ms

    def last_frame_age_ms(self) -> int | None:
        return self.frame_age_ms

    def last_msg_age_ms(self) -> int:
        # The construction-time grace the hb watchdog uses: always "fresh" here,
        # so a tick that read it would beat ws_data without any frame.
        return 0

    def reconnect_count_last_hour(self) -> int:
        return 3


def _daemon(probe: HealthProbe, readiness: TradingReadiness, **fields: Any) -> Any:
    fake = SimpleNamespace(probe=probe, trading_readiness=readiness, **fields)
    fake._dependency_answered = lambda dependency: Daemon._dependency_answered(fake, dependency)
    return fake


def _tick(probe: HealthProbe, readiness: TradingReadiness, ws: _WS) -> None:
    Daemon._ws_freshness_tick(_daemon(probe, readiness, ws_client=ws))


def _monitor(probe: HealthProbe, readiness: TradingReadiness) -> HealthMonitor:
    return HealthMonitor(phase=Phase.LIVE, event_sink=_Sink(), probe=probe, readiness=readiness)


def _fresh_liveness(probe: HealthProbe) -> None:
    now = datetime.now(UTC)
    for task in LIVENESS_THRESHOLDS:
        probe.last_active_ts[task] = now


async def _gate(safety_allowed: bool, guard_reason: str | None, *, audit: Any,
                readiness: TradingReadiness) -> object:
    candidate = make_candidate()
    gate = ExecutionGate(policy=ExecutionPolicy.BOOK_GUARDED, audit=audit, readiness=readiness)
    return await gate.prepare(
        candidate,
        decision_id="decision-outage",
        reconcile_id="reconcile-outage",
        snapshot=make_snapshot(),
        price=make_price(),
        fill_evidence=None,
        safety=GuardResult(safety_allowed, "heartbeat", guard_reason),
        audit_context=make_audit_context(candidate),
    )


class _AcceptingAudit:
    async def record(self, decision: object) -> None:
        return None


class _AcceptingAudit:
    async def record(self, decision: object) -> None:
        return None


def _guard(probe: HealthProbe) -> HeartbeatGuard:
    return HeartbeatGuard(probe=probe, watched_sub_tasks=[MARKET_DATA_FRESHNESS])


async def test_nothing_trades_before_the_first_market_data_frame_since_boot() -> None:
    """Never seen since boot is stale: the guard blocks and /readyz is not ready.
    The first frame lifts both with no decision, scan or restart involved, so the
    boot cannot wait on itself."""
    probe = HealthProbe()
    _fresh_liveness(probe)
    readiness = TradingReadiness(dependencies=[MARKET_DATA_FRESHNESS])
    readiness.set_ready()
    guard = _guard(probe)

    _tick(probe, readiness, _WS(frame_age_ms=None))  # connected, nothing received
    verdict = await guard.evaluate(make_candidate(), None)  # type: ignore[arg-type]
    assert verdict.allowed is False
    assert verdict.reason == "sub_task=ws_data never recorded since boot"
    assert readiness.snapshot().dependency == MARKET_DATA_FRESHNESS
    result = await _gate(verdict.allowed, verdict.reason, audit=_AcceptingAudit(),
                         readiness=readiness)
    assert isinstance(result, BlockedExecution)

    _tick(probe, readiness, _WS(frame_age_ms=500))  # first frame
    assert (await guard.evaluate(make_candidate(), None)).allowed is True  # type: ignore[arg-type]
    assert readiness.snapshot().reason != "dependency_stale"


async def test_a_bitfinex_ws_outage_keeps_liveness_and_blocks_trading_through_readiness() -> None:
    probe = HealthProbe()
    _fresh_liveness(probe)
    outage_began = datetime.now(UTC) - timedelta(minutes=10)
    # Before the outage both beats came from the same fresh frame.
    probe.last_active_ts["ws"] = outage_began
    probe.last_active_ts[MARKET_DATA_FRESHNESS] = outage_began
    readiness = TradingReadiness()
    readiness.set_ready()

    _tick(probe, readiness, _WS(frame_age_ms=10 * 60 * 1000))

    stale = await _monitor(probe, readiness).scan_staleness()  # must not raise FatalError
    assert [record["sub_task"] for record in stale] == [MARKET_DATA_FRESHNESS]
    client = TestClient(make_app(probe, readiness=readiness))
    assert client.get("/healthz").status_code == 200
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json() == {"trading_ready": False, "reason": "dependency_stale"}

    guard = _guard(probe)
    verdict = await guard.evaluate(make_candidate(), None)  # type: ignore[arg-type]
    assert verdict.allowed is False
    result = await _gate(verdict.allowed, verdict.reason, audit=_AcceptingAudit(),
                         readiness=readiness)
    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.SAFETY_GUARD_BLOCKED

    # Frames again: the very tick that sees them clears the dependency, without a
    # scan or a restart; readiness falls back to the last decision (blocked above).
    _tick(probe, readiness, _WS(frame_age_ms=1_000))
    assert readiness.snapshot().reason == BlockReason.SAFETY_GUARD_BLOCKED.value
    assert (await guard.evaluate(make_candidate(), None)).allowed is True  # type: ignore[arg-type]


class _Engine:
    """Answers or refuses ``SELECT 1``; the first attempt also ends the keepalive loop."""

    def __init__(self, stop: asyncio.Event, *, reachable: bool) -> None:
        self._stop = stop
        self._reachable = reachable

    def connect(self) -> object:
        self._stop.set()
        if not self._reachable:
            raise ConnectionRefusedError("database is down")

        class _Conn:
            async def __aenter__(self) -> Any:
                return SimpleNamespace(execute=_noop)

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Conn()


async def _noop(*_: object) -> None:
    return None


async def _one_keepalive(probe: HealthProbe, readiness: TradingReadiness, *, reachable: bool) -> None:
    stop = asyncio.Event()
    fake = _daemon(probe, readiness, db_engine=_Engine(stop, reachable=reachable), _stop_event=stop)
    await Daemon._db_keepalive_loop(fake)


async def test_a_database_outage_keeps_liveness_and_blocks_trading_through_readiness() -> None:
    probe = HealthProbe()
    _fresh_liveness(probe)
    outage_began = datetime.now(UTC) - timedelta(hours=1)
    # Before the outage both beats came from the same successful ping.
    probe.last_active_ts["db_keepalive"] = outage_began
    probe.last_active_ts[DB_FRESHNESS] = outage_began
    readiness = TradingReadiness()
    readiness.set_ready()

    await _one_keepalive(probe, readiness, reachable=False)

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

    # The database answers again: the successful ping itself clears /readyz.
    await _one_keepalive(probe, readiness, reachable=True)
    assert readiness.snapshot().reason != "dependency_stale"
