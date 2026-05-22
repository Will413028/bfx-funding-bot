from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.events import (
    CancelRequested,
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)


def test_reservation_claimed_optional_fields_default_none() -> None:
    e = ReservationClaimed(
        cid=42,
        venue_offer_id="v1",
        size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.venue_seq is None
    assert e.event_seq is None
    assert e.occurred_at_ms is None
    assert e.recorded_at_ms is None


def test_order_filled_optional_fields_default_none() -> None:
    e = OrderFilled(
        cid=42,
        venue_offer_id="v1",
        credit_id="C-1",
        size_usdt=Decimal("100"),
        fill_rate=0.0005,
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.venue_seq is None
    assert e.event_seq is None
    assert e.occurred_at_ms is None
    assert e.recorded_at_ms is None


def test_reservation_released_optional_fields_default_none() -> None:
    e = ReservationReleased(
        cid=42,
        venue_offer_id="v1",
        size_usdt=Decimal("100"),
        reason="user_cancel",
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.venue_seq is None
    assert e.event_seq is None


def test_cancel_requested_minimal() -> None:
    cid_uuid = uuid4()
    e = CancelRequested(
        venue_offer_id="v1",
        requested_at_ms=1000,
        signal_correlation_id=cid_uuid,
        account_id="default",
    )
    assert e.venue_offer_id == "v1"
    assert e.requested_at_ms == 1000
    assert e.signal_correlation_id == cid_uuid
    assert e.venue_seq is None
    assert e.event_seq is None
    assert e.occurred_at_ms is None
    assert e.recorded_at_ms is None


def test_events_are_frozen() -> None:
    import dataclasses

    e = ReservationClaimed(
        cid=42,
        venue_offer_id="v1",
        size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.cid = 99  # type: ignore[misc]
