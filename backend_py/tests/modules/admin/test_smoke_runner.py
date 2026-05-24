"""SmokeRunner L2 (in-process verification) and L3 (PG event_log read-your-writes) tests."""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.admin.smoke_runner import (
    SMOKE_ACCOUNT_ID,
    SMOKE_SIZE_USDT,
    SmokeRunner,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
)
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.protocols import (
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    Phase,
    StrategyName,
)


class _FakeEventSink:
    """Records all emit() calls in-memory. Used by EchoPaperExecutor."""

    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emits.append(event)


class _StubEventLogQuery:
    """Stub for _EventLogQueryProtocol — returns pre-configured rows (UPPERCASE event_type)."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[tuple[str, datetime]] = []

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        self.calls.append((account_id, since))
        return self.events


def _build_chain(bus: DomainEventBus, axiom: _FakeEventSink):
    """Build wrapped chain with paper executor + ReservationEmittingMiddleware."""
    paper = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    return ReservationEmittingMiddleware(paper, bus=bus, persister=NoopEventPersister())


def _make_runner(executor=None, bus=None, axiom=None, pg_query=None) -> SmokeRunner:
    bus = bus or DomainEventBus()
    axiom = axiom or _FakeEventSink()
    return SmokeRunner(
        executor=executor or _build_chain(bus, axiom),
        bus=bus,
        pg_query=pg_query or _StubEventLogQuery([]),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )


async def test_run_l2_happy_path_returns_pass() -> None:
    bus = DomainEventBus()
    axiom = _FakeEventSink()
    runner = _make_runner(bus=bus, axiom=axiom)

    result = await runner.run_l2()

    assert result.status == "pass"
    assert result.level == "L2"
    assert result.checks["executor_returned_filled"] is True
    assert result.checks["events_count"] == 2
    assert result.duration_ms >= 0
    assert result.error is None


async def test_run_l2_with_failing_executor_returns_fail() -> None:
    class _RaisingExecutor:
        async def submit(self, decision, ctx):
            raise RuntimeError("boom")

    runner = _make_runner(executor=_RaisingExecutor())
    result = await runner.run_l2()

    assert result.status == "fail"
    assert result.level == "L2"
    assert "boom" in (result.error or "")


async def test_run_l2_with_non_filled_executor_returns_fail() -> None:
    class _SubmittedOnlyExecutor:
        async def submit(self, decision, ctx):
            return SubmittedOrder(cid=1, venue_offer_id="x", status="submitted", raw_response=None)

    runner = _make_runner(executor=_SubmittedOnlyExecutor())
    result = await runner.run_l2()

    assert result.status == "fail"
    assert result.checks["executor_returned_filled"] is False


async def test_run_l2_emits_smoke_account_id_events() -> None:
    """L2 should produce events tagged with smoke_test account_id."""
    bus = DomainEventBus()
    axiom = _FakeEventSink()
    captured: list[Any] = []

    async def spy(e):
        captured.append(e)

    bus.subscribe(ReservationClaimed, spy)
    bus.subscribe(OrderFilled, spy)

    runner = _make_runner(bus=bus, axiom=axiom)
    result = await runner.run_l2()

    assert result.status == "pass"
    assert len(captured) == 2
    assert all(e.account_id == SMOKE_ACCOUNT_ID for e in captured)
    assert all(e.is_simulated for e in captured)
    assert captured[0].size_usdt == Decimal(str(SMOKE_SIZE_USDT))


async def test_run_l2_single_flight_serializes() -> None:
    """Two concurrent run_l2 calls must serialize via _SMOKE_LOCK."""
    runner = _make_runner()
    r1, r2 = await asyncio.gather(runner.run_l2(), runner.run_l2())
    assert r1.status == "pass"
    assert r2.status == "pass"


def _make_pg_row(event_type: str) -> dict[str, Any]:
    """Build a row dict matching PostgresEventLogQueryAdapter output (UPPERCASE event_type)."""
    return {
        "event_type": event_type,   # UPPERCASE — matches PG event_log storage
        "account_id": SMOKE_ACCOUNT_ID,
        "occurred_at_ms": 1716374400000,
    }


async def test_run_l3_happy_returns_pass() -> None:
    # UPPERCASE event_type — exactly what PostgresEventLogQueryAdapter returns
    pg_query = _StubEventLogQuery([
        _make_pg_row("RESERVATION_CLAIMED"),
        _make_pg_row("ORDER_FILL"),
    ])
    runner = _make_runner(pg_query=pg_query)

    result = await runner.run_l3()

    assert result.status == "pass"
    assert result.level == "L3"
    assert result.checks["l2_passed"] is True
    assert result.checks["pg_events_seen"] == 2
    assert set(result.checks["pg_event_types"]) == {"RESERVATION_CLAIMED", "ORDER_FILL"}
    assert len(pg_query.calls) >= 1
    assert pg_query.calls[0][0] == SMOKE_ACCOUNT_ID


async def test_run_l3_pg_returns_empty_fails_after_poll_timeout() -> None:
    """L2 passes but PG event_log never returns events → L3 fail."""
    pg_query = _StubEventLogQuery([])  # always empty
    runner = _make_runner(pg_query=pg_query)

    # Override poll budget for speed (test does not wait 15s)
    result = await runner._run_l3_unlocked(
        poll_attempts=2, poll_interval_s=0.01,
    )

    assert result.status == "fail"
    assert result.level == "L3"
    assert result.checks["l2_passed"] is True
    assert "round-trip" in (result.error or "").lower()


async def test_run_l3_pg_returns_only_one_event_type_fails() -> None:
    pg_query = _StubEventLogQuery([
        _make_pg_row("RESERVATION_CLAIMED"),
        # missing ORDER_FILL
    ])
    runner = _make_runner(pg_query=pg_query)
    result = await runner._run_l3_unlocked(poll_attempts=2, poll_interval_s=0.01)

    assert result.status == "fail"
    assert result.level == "L3"


async def test_run_l3_with_failing_l2_short_circuits() -> None:
    """If L2 fails, L3 should return immediately without polling PG."""
    class _RaisingExecutor:
        async def submit(self, decision, ctx):
            raise RuntimeError("boom")

    pg_query = _StubEventLogQuery([])
    runner = _make_runner(executor=_RaisingExecutor(), pg_query=pg_query)
    result = await runner.run_l3()

    assert result.status == "fail"
    assert result.level == "L2"  # L3 short-circuited; result reflects L2 failure
    assert pg_query.calls == []  # no PG poll happened
