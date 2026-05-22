"""SmokeRunner L2 (in-process verification) tests."""
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
    EventType,
    Phase,
    StrategyName,
)


class _FakeAxiomClient:
    """Records all emit() calls in-memory. Conforms to _AxiomProtocol."""

    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emits.append(event)


class _FakeAxiomQuery:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[tuple[str, datetime]] = []

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        self.calls.append((account_id, since))
        return self.events


def _build_chain(bus: DomainEventBus, axiom: _FakeAxiomClient):
    """Build wrapped chain with paper executor + ReservationEmittingMiddleware."""
    paper = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    return ReservationEmittingMiddleware(paper, bus=bus)


def _make_runner(executor=None, bus=None, axiom=None, axiom_query=None) -> SmokeRunner:
    bus = bus or DomainEventBus()
    axiom = axiom or _FakeAxiomClient()
    return SmokeRunner(
        executor=executor or _build_chain(bus, axiom),
        bus=bus,
        axiom_client=axiom,
        axiom_query=axiom_query or _FakeAxiomQuery([]),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )


async def test_run_l2_happy_path_returns_pass() -> None:
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
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
    axiom = _FakeAxiomClient()
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


def _make_axiom_row(event_type: str) -> dict[str, Any]:
    return {
        "_time": "2026-05-22T10:00:00Z",
        "event_type": event_type,
        "account_id": SMOKE_ACCOUNT_ID,
        "payload": {"size_usdt": 1.0},
    }


async def test_run_l3_happy_returns_pass() -> None:
    axiom_query = _FakeAxiomQuery([
        _make_axiom_row(EventType.RESERVATION_CLAIMED.value),
        _make_axiom_row(EventType.ORDER_FILL.value),
    ])
    runner = _make_runner(axiom_query=axiom_query)

    result = await runner.run_l3()

    assert result.status == "pass"
    assert result.level == "L3"
    assert result.checks["l2_passed"] is True
    assert result.checks["axiom_events_seen"] == 2
    assert len(axiom_query.calls) >= 1
    assert axiom_query.calls[0][0] == SMOKE_ACCOUNT_ID


async def test_run_l3_axiom_returns_empty_fails_after_poll_timeout() -> None:
    """L2 passes but Axiom never returns events → L3 fail."""
    axiom_query = _FakeAxiomQuery([])  # always empty
    runner = _make_runner(axiom_query=axiom_query)

    # Override poll budget for speed (test does not wait 15s)
    result = await runner._run_l3_unlocked(
        poll_attempts=2, poll_interval_s=0.01,
    )

    assert result.status == "fail"
    assert result.level == "L3"
    assert result.checks["l2_passed"] is True
    assert "round-trip" in (result.error or "").lower() or \
           "axiom" in (result.error or "").lower()


async def test_run_l3_axiom_returns_only_one_event_type_fails() -> None:
    axiom_query = _FakeAxiomQuery([
        _make_axiom_row(EventType.RESERVATION_CLAIMED.value),
        # missing ORDER_FILL
    ])
    runner = _make_runner(axiom_query=axiom_query)
    result = await runner._run_l3_unlocked(poll_attempts=2, poll_interval_s=0.01)

    assert result.status == "fail"
    assert result.level == "L3"


async def test_run_l3_with_failing_l2_short_circuits() -> None:
    """If L2 fails, L3 should return immediately without polling Axiom."""
    class _RaisingExecutor:
        async def submit(self, decision, ctx):
            raise RuntimeError("boom")

    axiom_query = _FakeAxiomQuery([])
    runner = _make_runner(executor=_RaisingExecutor(), axiom_query=axiom_query)
    result = await runner.run_l3()

    assert result.status == "fail"
    assert result.level == "L2"  # L3 short-circuited; result reflects L2 failure
    assert axiom_query.calls == []  # no Axiom poll happened
