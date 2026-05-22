"""Phase 4.3 schemas additions: new EventType values, new payload models,
HealthTarget.LEDGER.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthTarget,
    ReservationClaimedPayload,
    ReservationReleasedPayload,
)


def test_event_type_has_reservation_claimed() -> None:
    assert EventType.RESERVATION_CLAIMED.value == "reservation_claimed"


def test_event_type_has_reservation_released() -> None:
    assert EventType.RESERVATION_RELEASED.value == "reservation_released"


def test_health_target_has_ledger() -> None:
    assert HealthTarget.LEDGER.value == "ledger"


def test_reservation_claimed_payload_roundtrip() -> None:
    payload = ReservationClaimedPayload(
        cid=42,
        venue_offer_id="paper_abc",
        size_usdt=100.0,
        signal_correlation_id=uuid4(),
        is_simulated=True,
    )
    data = payload.model_dump()
    assert data["cid"] == 42
    assert data["venue_offer_id"] == "paper_abc"
    assert data["size_usdt"] == 100.0


def test_reservation_released_payload_requires_reason() -> None:
    with pytest.raises(ValidationError):
        ReservationReleasedPayload(  # type: ignore[call-arg]
            cid=1, venue_offer_id="x", size_usdt=1.0,
            signal_correlation_id=uuid4(), is_simulated=True,
        )


def test_reservation_released_payload_valid_reasons() -> None:
    for reason in ("venue_cancel", "user_cancel", "expired", "missing_from_venue"):
        payload = ReservationReleasedPayload(
            cid=1, venue_offer_id="x", size_usdt=1.0, reason=reason,
            signal_correlation_id=uuid4(), is_simulated=True,
        )
        assert payload.reason == reason
