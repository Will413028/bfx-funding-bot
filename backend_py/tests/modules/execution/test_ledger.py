"""PaperPositionLedger: event-sourcing replay + on_order_fill listener."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import LedgerReplayError
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    OrderFillPayload,
)


class _FakeAxiomQuery:
    def __init__(self, events: list[dict[str, Any]] | Exception) -> None:
        self._events = events

    async def query_order_fills(
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
async def test_replay_sums_fills() -> None:
    events = [_fill_event(100.0), _fill_event(50.0), _fill_event(25.5)]
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery(events),
    )
    assert ledger.current_exposure() == Decimal("175.5")


@pytest.mark.asyncio
async def test_on_order_fill_mutates_exposure() -> None:
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery([]),
    )
    payload = OrderFillPayload(
        cid=1, offer_id="x", signal_correlation_id=uuid4(),
        fill_size_usdt=200.0, fill_price=0.0001, is_simulated=True,
    )
    ledger.on_order_fill(payload)
    assert ledger.current_exposure() == Decimal("200")


@pytest.mark.asyncio
async def test_replay_fails_fast_on_axiom_error() -> None:
    with pytest.raises(LedgerReplayError):
        await PaperPositionLedger.replay_from_axiom(
            account_id="default", since=datetime.now(UTC) - timedelta(days=30),
            axiom_query=_FakeAxiomQuery(RuntimeError("axiom unreachable")),
            max_retries=2,
        )
