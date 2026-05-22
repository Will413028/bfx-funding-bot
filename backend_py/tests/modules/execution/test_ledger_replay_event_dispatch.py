"""Replay event-type dispatch (Phase 4.3): RESERVATION_CLAIMED / ORDER_FILL /
RESERVATION_RELEASED dispatch + defensive sort + floor count.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.marketfeed.schemas import EventType


class _FakeAxiomQuery:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        return self._events


def _ev(event_type: EventType, ts: str, size: float, account_id: str = "default",
        reason: str = "venue_cancel") -> dict[str, Any]:
    if event_type == EventType.ORDER_FILL:
        payload = {
            "cid": 1,
            "offer_id": "x",
            "signal_correlation_id": str(uuid4()),
            "fill_size_usdt": size,
            "fill_price": 0.0001,
            "is_simulated": True,
        }
    elif event_type == EventType.RESERVATION_RELEASED:
        payload = {
            "cid": 1,
            "venue_offer_id": "x",
            "size_usdt": size,
            "reason": reason,
            "signal_correlation_id": str(uuid4()),
            "is_simulated": True,
        }
    else:  # RESERVATION_CLAIMED
        payload = {
            "cid": 1,
            "venue_offer_id": "x",
            "size_usdt": size,
            "signal_correlation_id": str(uuid4()),
            "is_simulated": True,
        }
    return {
        "event_type": event_type.value,
        "account_id": account_id,
        "_time": ts,
        "payload": payload,
    }


@pytest.mark.asyncio
async def test_replay_dispatches_mixed_events_in_order() -> None:
    events = [
        _ev(EventType.RESERVATION_CLAIMED, "2026-05-22T00:00:00Z", 100),
        _ev(EventType.RESERVATION_CLAIMED, "2026-05-22T00:01:00Z", 200),
        _ev(EventType.ORDER_FILL,          "2026-05-22T00:02:00Z", 100),
        _ev(EventType.RESERVATION_RELEASED, "2026-05-22T00:03:00Z", 200),
    ]
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery(events),
    )
    assert ledger._reserved == Decimal("0")
    assert ledger._realized == Decimal("100")
    assert ledger.current_exposure() == Decimal("100")
    assert ledger.replay_floor_hit_count == 0


@pytest.mark.asyncio
async def test_replay_defensive_sorts_out_of_order_input() -> None:
    events = [
        _ev(EventType.RESERVATION_RELEASED, "2026-05-22T00:03:00Z", 200),
        _ev(EventType.RESERVATION_CLAIMED, "2026-05-22T00:00:00Z", 100),
        _ev(EventType.ORDER_FILL,          "2026-05-22T00:02:00Z", 100),
        _ev(EventType.RESERVATION_CLAIMED, "2026-05-22T00:01:00Z", 200),
    ]
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery(events),
    )
    assert ledger._reserved == Decimal("0")
    assert ledger._realized == Decimal("100")
    assert ledger.replay_floor_hit_count == 0


@pytest.mark.asyncio
async def test_replay_floor_counts_release_without_claim() -> None:
    events = [
        # CLAIMED out of 30d window → only RELEASED + FILL in stream
        _ev(EventType.RESERVATION_RELEASED, "2026-05-22T00:00:00Z", 50),
        _ev(EventType.ORDER_FILL,           "2026-05-22T00:01:00Z", 100),
    ]
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery(events),
    )
    assert ledger._reserved == Decimal("0")
    assert ledger._realized == Decimal("100")
    assert ledger.replay_floor_hit_count == 2  # release + fill both flooring


@pytest.mark.asyncio
async def test_replay_skips_unknown_event_type(caplog: pytest.LogCaptureFixture) -> None:
    events: list[dict[str, Any]] = [
        {"event_type": "decision", "account_id": "default",
         "_time": "2026-05-22T00:00:00Z", "payload": {}},
        _ev(EventType.RESERVATION_CLAIMED, "2026-05-22T00:01:00Z", 100),
    ]
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery(events),
    )
    assert ledger._reserved == Decimal("100")


@pytest.mark.asyncio
async def test_replay_skips_wrong_account_id() -> None:
    events = [
        _ev(EventType.RESERVATION_CLAIMED, "2026-05-22T00:00:00Z", 100, account_id="other"),
        _ev(EventType.RESERVATION_CLAIMED, "2026-05-22T00:01:00Z", 50,  account_id="default"),
    ]
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery(events),
    )
    assert ledger._reserved == Decimal("50")


@pytest.mark.asyncio
async def test_replay_malformed_payload_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = [
        {"event_type": EventType.RESERVATION_CLAIMED.value, "account_id": "default",
         "_time": "2026-05-22T00:00:00Z",
         "payload": {"cid": "not-an-int"}},  # malformed
        _ev(EventType.RESERVATION_CLAIMED, "2026-05-22T00:01:00Z", 100),
    ]
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default", since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeAxiomQuery(events),
    )
    assert ledger._reserved == Decimal("100")
    assert "ledger_replay_skip_malformed" in caplog.text
