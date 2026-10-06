from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
    PositionReconciled,
)


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
    assert e.occurred_at_ms is None


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
    assert event.occurred_at_ms is None


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


def test_position_reconciled_has_symbol_and_native_fields() -> None:
    e = PositionReconciled(
        account_id="default",
        symbol="fUST",
        reserved=Decimal("300"),
        realized=Decimal("450"),
        available=Decimal("19.10"),
        n_offers=2,
        n_credits=3,
        occurred_at_ms=1000,
    )
    assert e.symbol == "fUST"
    assert e.reserved == Decimal("300")
    assert e.realized == Decimal("450")
    assert e.available == Decimal("19.10")
    # transitional read aliases still resolve
    assert e.reserved_usdt == Decimal("300")
    assert e.realized_usdt == Decimal("450")
    assert e.available_usdt == Decimal("19.10")


def test_position_reconciled_requires_symbol() -> None:
    # symbol is now mandatory (no default) — omitting it raises TypeError.
    with pytest.raises(TypeError):
        PositionReconciled(  # type: ignore[call-arg]
            account_id="default",
            reserved=Decimal("300"),
            realized=Decimal("450"),
            available=Decimal("19.10"),
            n_offers=2,
            n_credits=3,
            occurred_at_ms=1000,
        )


def test_position_reconciled_back_compat_usdt_kwargs() -> None:
    # the legacy reconcile's *_usdt= keyword spelling is still accepted
    e = PositionReconciled(
        account_id="default",
        symbol="fUSD",
        reserved_usdt=Decimal("300"),
        realized_usdt=Decimal("450"),
        available_usdt=Decimal("19.10"),
        n_offers=2,
        n_credits=3,
        occurred_at_ms=1000,
    )
    assert e.reserved == Decimal("300")
    assert e.realized == Decimal("450")
    assert e.available == Decimal("19.10")


def test_position_reconciled_rejects_conflicting_canonical_and_usdt() -> None:
    with pytest.raises(TypeError, match="disagree"):
        PositionReconciled(
            account_id="default", symbol="fUST",
            n_offers=0, n_credits=0, occurred_at_ms=0,
            reserved=Decimal("300"), reserved_usdt=Decimal("350"),
            realized=Decimal("0"), available=Decimal("0"),
        )
