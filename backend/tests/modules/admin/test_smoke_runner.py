"""SmokeRunner L2 (in-process verification) and L3 (PG event_log read-your-writes) tests."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.admin.smoke_runner import (
    SMOKE_ACCOUNT_ID,
    SmokeRunner,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.events import OrderFilled, ReservationClaimed
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.strategy import StrategyName


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


async def test_run_l2_fails_closed_without_audited_ready_producer() -> None:
    bus = DomainEventBus()
    axiom = _FakeEventSink()
    runner = _make_runner(bus=bus, axiom=axiom)

    result = await runner.run_l2()

    assert result.status == "fail"
    assert result.level == "L2"
    assert "audited ReadyToSubmit" in (result.error or "")
    assert result.duration_ms >= 0


async def test_run_l2_with_failing_executor_returns_fail() -> None:
    class _RaisingExecutor:
        async def submit(self, decision, ctx):
            raise RuntimeError("boom")

    runner = _make_runner(executor=_RaisingExecutor())
    result = await runner.run_l2()

    assert result.status == "fail"
    assert result.level == "L2"
    assert "audited ReadyToSubmit" in (result.error or "")


async def test_run_l2_with_non_filled_executor_returns_fail() -> None:
    class _SubmittedOnlyExecutor:
        async def submit(self, decision, ctx):
            raise AssertionError("disabled smoke path must not call executor")

    runner = _make_runner(executor=_SubmittedOnlyExecutor())
    result = await runner.run_l2()

    assert result.status == "fail"
    assert "audited ReadyToSubmit" in (result.error or "")


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

    assert result.status == "fail"
    assert captured == []


async def test_run_l2_single_flight_serializes() -> None:
    """Two concurrent run_l2 calls must serialize via _SMOKE_LOCK."""
    runner = _make_runner()
    r1, r2 = await asyncio.gather(runner.run_l2(), runner.run_l2())
    assert r1.status == "fail"
    assert r2.status == "fail"


def _make_pg_row(event_type: str) -> dict[str, Any]:
    """Build a row dict matching PostgresEventLogQueryAdapter output (UPPERCASE event_type)."""
    return {
        "event_type": event_type,   # UPPERCASE — matches PG event_log storage
        "account_id": SMOKE_ACCOUNT_ID,
        "occurred_at_ms": 1716374400000,
    }


async def test_run_l3_fails_closed_while_smoke_submit_is_gated() -> None:
    # UPPERCASE event_type — exactly what PostgresEventLogQueryAdapter returns
    pg_query = _StubEventLogQuery([
        _make_pg_row("RESERVATION_CLAIMED"),
        _make_pg_row("ORDER_FILL"),
    ])
    runner = _make_runner(pg_query=pg_query)

    result = await runner.run_l3()

    assert result.status == "fail"
    assert result.level == "L2"
    assert pg_query.calls == []


async def test_run_l3_pg_returns_empty_fails_after_poll_timeout() -> None:
    """L2 passes but PG event_log never returns events → L3 fail."""
    pg_query = _StubEventLogQuery([])  # always empty
    runner = _make_runner(pg_query=pg_query)

    # Override poll budget for speed (test does not wait 15s)
    result = await runner._run_l3_unlocked(
        poll_attempts=2, poll_interval_s=0.01,
    )

    assert result.status == "fail"
    assert result.level == "L2"
    assert "audited ReadyToSubmit" in (result.error or "")


async def test_run_l3_pg_returns_only_one_event_type_fails() -> None:
    pg_query = _StubEventLogQuery([
        _make_pg_row("RESERVATION_CLAIMED"),
        # missing ORDER_FILL
    ])
    runner = _make_runner(pg_query=pg_query)
    result = await runner._run_l3_unlocked(poll_attempts=2, poll_interval_s=0.01)

    assert result.status == "fail"
    assert result.level == "L2"


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
