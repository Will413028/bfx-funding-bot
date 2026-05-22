"""PaperPositionLedger: event-sourcing replay tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import LedgerReplayError
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.marketfeed.schemas import EventType


class _FakeAxiomQuery:
    def __init__(self, events: list[dict[str, Any]] | Exception) -> None:
        self._events = events

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        if isinstance(self._events, Exception):
            raise self._events
        return self._events


def _fill_event(size: float) -> dict[str, Any]:
    return {
        "event_type": EventType.ORDER_FILL.value,
        "account_id": "default",
        "payload": {
            "cid": 1, "offer_id": "x",
            "signal_correlation_id": str(uuid4()),
            "fill_size_usdt": size, "fill_price": 0.0001,
            "is_simulated": True,
        },
    }


@pytest.mark.asyncio
async def test_replay_empty_returns_zero_exposure() -> None:
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery([]),
    )
    assert ledger.current_exposure() == Decimal("0")


@pytest.mark.asyncio
async def test_replay_order_fills_only_with_floor() -> None:
    """Legacy ORDER_FILL events without prior CLAIMED → realized += size,
    reserved floor-at-0 (Phase 4.3 dual counter semantics).
    """
    events = [_fill_event(100.0), _fill_event(50.0), _fill_event(25.5)]
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery(events),
    )
    assert ledger.current_exposure() == Decimal("175.5")
    assert ledger.realized_exposure() == Decimal("175.5")
    assert ledger.replay_floor_hit_count == 3  # all 3 fills hit reserved floor


@pytest.mark.asyncio
async def test_replay_fails_fast_on_axiom_error() -> None:
    with pytest.raises(LedgerReplayError):
        await PaperPositionLedger.replay_from_axiom(
            account_id="default", since=datetime.now(UTC) - timedelta(days=30),
            axiom_query=_FakeAxiomQuery(RuntimeError("axiom unreachable")),
            max_retries=2,
        )


async def test_handler_skips_foreign_account_id_claim() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_reservation_claimed(foreign)
    assert ledger.current_exposure() == Decimal("0")


async def test_handler_skips_foreign_account_id_fill() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = OrderFilled(
        cid=1, venue_offer_id="x", credit_id=None, size_usdt=Decimal("100"),
        fill_rate=0.0001, signal_correlation_id=uuid4(),
        account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_order_filled(foreign)
    assert ledger.realized_exposure() == Decimal("0")
    assert ledger.current_exposure() == Decimal("0")
    assert ledger.replay_floor_hit_count == 0


async def test_handler_skips_foreign_account_id_release() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = ReservationReleased(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_reservation_released(foreign)
    assert ledger.current_exposure() == Decimal("0")
    assert ledger.replay_floor_hit_count == 0


async def test_handler_processes_matching_account_id_unchanged() -> None:
    """Regression: matching account_id still updates counters as before."""
    ledger = PaperPositionLedger(account_id="default")
    matching = ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=True,
    )
    await ledger.on_reservation_claimed(matching)
    assert ledger.current_exposure() == Decimal("100")
