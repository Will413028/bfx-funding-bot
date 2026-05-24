from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import (
    OfferRegistry,
    RegistryState,
)


@pytest.mark.asyncio
async def test_handle_claim_adds_record() -> None:
    reg = OfferRegistry(clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=500,
    ))
    snap = reg.snapshot()
    assert "v1" in snap
    assert snap["v1"].state == RegistryState.CLAIMED


@pytest.mark.asyncio
async def test_snapshot_is_immutable_view() -> None:
    reg = OfferRegistry(clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
    ))
    snap = reg.snapshot()
    snap["v2"] = "tampered"  # type: ignore[assignment]
    assert "v2" not in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_keeps_recent_released_records() -> None:
    """Within TTL — RELEASED record kept."""
    reg = OfferRegistry(clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=500,
    ))
    await reg.handle(ReservationReleased(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        reason="user_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=False, occurred_at_ms=900,
    ))
    assert reg.snapshot()["v1"].state == RegistryState.RELEASED
    # clock=1000, last_updated_ms=1000, threshold = 1000 - 86_400_000 = very negative
    # record's last_updated >= threshold, so kept
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v1" in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_preserves_claimed_records() -> None:
    """CLAIMED never cleaned, regardless of age."""
    reg = OfferRegistry(clock=lambda: 1_000_000_000_000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=0,
    ))
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v1" in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_removes_old_released_records() -> None:
    """Past TTL — RELEASED record removed."""
    reg = OfferRegistry(clock=lambda: 100_000_000_000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v_old", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=100,
    ))
    # release at clock=100_000_000_000
    await reg.handle(ReservationReleased(
        cid=42, venue_offer_id="v_old", size_usdt=Decimal("100"),
        reason="user_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=False, occurred_at_ms=200,
    ))
    # Move clock forward by 25hr
    reg._clock = lambda: 100_000_000_000 + 25 * 3600 * 1000  # type: ignore[method-assign]
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v_old" not in reg.snapshot()


def test_parse_event_order_fill_uses_legacy_field_names() -> None:
    corr_id = uuid4()
    row = {
        "_time": "2026-05-23T11:00:00Z",
        "event_type": "order_fill",
        "account_id": "default",
        "correlation_id": str(corr_id),
        "payload": {
            "cid": 12345,
            "offer_id": "v1",            # legacy name (mirror ledger.py:167)
            "signal_correlation_id": str(corr_id),
            "fill_size_usdt": 150.0,     # legacy name
            "fill_price": 0.00012,        # legacy name
            "is_simulated": False,
        },
    }
    event = OfferRegistry._parse_event(row)
    assert isinstance(event, OrderFilled)
    assert event.venue_offer_id == "v1"
    assert event.size_usdt == Decimal("150.0")
    assert event.fill_rate == 0.00012
    assert event.credit_id is None  # not in legacy schema
    assert event.account_id == "default"  # from row root
    assert event.is_simulated is False
    assert event.cid == 12345


def test_parse_event_reservation_released_uses_v2_names() -> None:
    corr_id = uuid4()
    row = {
        "_time": "2026-05-23T12:00:00Z",
        "event_type": "reservation_released",
        "account_id": "default",
        "correlation_id": str(corr_id),
        "payload": {
            "cid": 12345,
            "venue_offer_id": "v1",
            "size_usdt": 150.0,
            "reason": "user_cancel",
            "signal_correlation_id": str(corr_id),
            "is_simulated": False,
        },
    }
    event = OfferRegistry._parse_event(row)
    assert isinstance(event, ReservationReleased)
    assert event.venue_offer_id == "v1"
    assert event.size_usdt == Decimal("150.0")
    assert event.reason == "user_cancel"
    assert event.account_id == "default"


def test_parse_event_unknown_type_returns_none() -> None:
    row = {
        "_time": "2026-05-23T13:00:00Z",
        "event_type": "signal",  # not a replay event
        "account_id": "default",
        "correlation_id": "uuid",
        "payload": {},
    }
    assert OfferRegistry._parse_event(row) is None


def test_parse_event_account_id_falls_back_to_payload_when_row_root_missing() -> None:
    corr_id = uuid4()
    row = {
        "_time": "2026-05-23T11:00:00Z",
        "event_type": "order_fill",
        "correlation_id": str(corr_id),
        # No account_id at row root (legacy data shape)
        "payload": {
            "cid": 1, "offer_id": "v1", "signal_correlation_id": str(corr_id),
            "fill_size_usdt": 50.0, "fill_price": 0.0001, "is_simulated": True,
            "account_id": "legacy-acct",
        },
    }
    event = OfferRegistry._parse_event(row)
    assert event.account_id == "legacy-acct"
