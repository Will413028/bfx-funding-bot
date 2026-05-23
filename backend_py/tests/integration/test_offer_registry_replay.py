"""End-to-end OfferRegistry.replay_from_axiom — fake adapter returns mixed events."""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from bfx_funding_bot.modules.execution.registry_offers import (
    OfferRegistry,
    RegistryState,
)

pytestmark = pytest.mark.integration


class _CannedAdapter:
    """Returns canned events for replay_from_axiom test."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._rows


@pytest.mark.asyncio
async def test_replay_rebuilds_state_from_three_event_types() -> None:
    corr_a = str(uuid.uuid4())
    corr_b = str(uuid.uuid4())
    rows = [
        {
            "_time": "2026-05-23T10:00:00Z",
            "event_type": "reservation_claimed",
            "occurred_at_ms": 1700000000000,
            "account_id": "default",
            "payload": {
                "cid": 1, "venue_offer_id": "voi-A", "size_usdt": 100.0,
                "signal_correlation_id": corr_a, "account_id": "default",
                "is_simulated": False, "occurred_at_ms": 1700000000000,
            },
        },
        {
            "_time": "2026-05-23T11:00:00Z",
            "event_type": "reservation_claimed",
            "occurred_at_ms": 1700000003600000,
            "account_id": "default",
            "payload": {
                "cid": 2, "venue_offer_id": "voi-B", "size_usdt": 200.0,
                "signal_correlation_id": corr_b, "account_id": "default",
                "is_simulated": False, "occurred_at_ms": 1700000003600000,
            },
        },
        {
            "_time": "2026-05-23T11:30:00Z",
            "event_type": "order_fill",
            "occurred_at_ms": 1700000005400000,
            "account_id": "default",
            "payload": {
                "cid": 1, "offer_id": "voi-A",
                "signal_correlation_id": corr_a,
                "fill_size_usdt": 100.0, "fill_price": 0.0001,
                "is_simulated": False, "account_id": "default",
            },
        },
        {
            "_time": "2026-05-23T12:00:00Z",
            "event_type": "signal",  # not a replay event — skipped
            "occurred_at_ms": 1700000007200000,
            "account_id": "default",
            "payload": {"signal_score": 0.5},
        },
    ]
    registry = OfferRegistry(axiom_query=_CannedAdapter(rows), clock=lambda: 9999999)

    await registry.replay_from_axiom()
    snapshot = registry.snapshot()

    assert set(snapshot.keys()) == {"voi-A", "voi-B"}
    assert snapshot["voi-A"].state == RegistryState.RELEASED  # filled
    assert snapshot["voi-B"].state == RegistryState.CLAIMED   # still open
    assert snapshot["voi-A"].cid == 1
    assert snapshot["voi-B"].cid == 2


@pytest.mark.asyncio
async def test_replay_handles_empty_event_log() -> None:
    registry = OfferRegistry(axiom_query=_CannedAdapter([]), clock=lambda: 9999999)
    await registry.replay_from_axiom()
    assert registry.snapshot() == {}


@pytest.mark.asyncio
async def test_replay_sorts_by_time_then_event_seq() -> None:
    """Out-of-order rows must be re-sorted before applying."""
    corr = str(uuid.uuid4())
    rows = [
        {  # later release (should apply second after sort)
            "_time": "2026-05-23T11:00:00Z",
            "event_type": "reservation_released",
            "occurred_at_ms": 1700000003600000,
            "account_id": "default",
            "payload": {
                "cid": 1, "venue_offer_id": "voi-X", "size_usdt": 50.0,
                "reason": "user_cancel", "signal_correlation_id": corr,
                "is_simulated": False, "account_id": "default",
                "occurred_at_ms": 1700000003600000,
            },
        },
        {  # earlier claim (should apply first after sort)
            "_time": "2026-05-23T10:00:00Z",
            "event_type": "reservation_claimed",
            "occurred_at_ms": 1700000000000,
            "account_id": "default",
            "payload": {
                "cid": 1, "venue_offer_id": "voi-X", "size_usdt": 50.0,
                "signal_correlation_id": corr, "account_id": "default",
                "is_simulated": False, "occurred_at_ms": 1700000000000,
            },
        },
    ]
    registry = OfferRegistry(axiom_query=_CannedAdapter(rows), clock=lambda: 9999999)
    await registry.replay_from_axiom()
    snapshot = registry.snapshot()
    assert snapshot["voi-X"].state == RegistryState.RELEASED
