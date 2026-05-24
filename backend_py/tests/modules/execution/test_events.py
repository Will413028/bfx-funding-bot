from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
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


def test_cancel_acknowledged_constructs_with_required_fields() -> None:
    cid = uuid4()
    event = CancelAcknowledged(
        venue_offer_id="123",
        acknowledged_at_ms=1700000000000,
        signal_correlation_id=cid,
        account_id="default",
        rest_status="success",
    )
    assert event.venue_offer_id == "123"
    assert event.rest_status == "success"
    assert event.venue_response_text is None  # default
    assert event.venue_seq is None  # default (never WS-sourced)
    assert event.event_seq is None
    assert event.occurred_at_ms is None
    assert event.recorded_at_ms is None


def test_cancel_acknowledged_already_terminal_status() -> None:
    event = CancelAcknowledged(
        venue_offer_id="456",
        acknowledged_at_ms=1700000000000,
        signal_correlation_id=uuid4(),
        account_id="default",
        rest_status="already_terminal",
        venue_response_text="Offer not found",
    )
    assert event.rest_status == "already_terminal"
    assert event.venue_response_text == "Offer not found"


def test_cancel_acknowledged_is_frozen() -> None:
    import dataclasses
    event = CancelAcknowledged(
        venue_offer_id="123",
        acknowledged_at_ms=1700000000000,
        signal_correlation_id=uuid4(),
        account_id="default",
        rest_status="success",
    )
    try:
        event.venue_offer_id = "999"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("CancelAcknowledged should be frozen")


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


_SCID_T1 = UUID("11111111-1111-1111-1111-111111111111")


def test_reservation_intent_fields() -> None:
    ev = ReservationIntent(
        cid=42,
        size_usdt=Decimal("100"),
        signal_correlation_id=_SCID_T1,
        account_id="acct",
        is_simulated=True,
        occurred_at_ms=1000,
    )
    assert ev.cid == 42
    assert ev.size_usdt == Decimal("100")
    assert ev.account_id == "acct"
    # uniform bitemporal optionals default None
    assert ev.event_seq is None
    assert ev.recorded_at_ms is None
    # INTENT carries no venue_offer_id (PENDING — voi unknown until CLAIMED)
    assert not hasattr(ev, "venue_offer_id")


def test_reservation_failed_fields() -> None:
    ev = ReservationFailed(
        cid=42,
        size_usdt=Decimal("100"),
        signal_correlation_id=_SCID_T1,
        account_id="acct",
        is_simulated=False,
        reason="submit_failed",
        occurred_at_ms=2000,
    )
    assert ev.reason == "submit_failed"
    assert ev.is_simulated is False
