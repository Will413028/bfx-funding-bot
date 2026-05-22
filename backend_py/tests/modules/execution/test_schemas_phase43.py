"""Phase 4.3 schemas additions: new EventType values, new payload models,
HealthTarget.LEDGER.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.marketfeed.schemas import (
    Envelope,
    EventType,
    HealthTarget,
    Level,
    Phase,
    ReservationClaimedPayload,
    ReservationReleasedPayload,
    StrategyName,
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


# ---------- Envelope integration tests (validator wiring) ----------


def _envelope_kwargs(
    event_type: EventType, payload: dict[str, object]
) -> dict[str, object]:
    """Helper to build valid Envelope kwargs."""
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": Level.INFO.value,
        "phase": Phase.PAPER.value,
        "strategy": StrategyName.RATE_PERCENTILE.value,
        "cell": "bfx_USDT",
        "event_type": event_type.value,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def test_envelope_validates_reservation_claimed_payload() -> None:
    """Envelope._validate_payload routes RESERVATION_CLAIMED through ReservationClaimedPayload."""
    env = Envelope.model_validate(
        _envelope_kwargs(
            EventType.RESERVATION_CLAIMED,
            {
                "cid": 42,
                "venue_offer_id": "paper_xyz",
                "size_usdt": 250.0,
                "signal_correlation_id": str(uuid4()),
                "is_simulated": True,
            },
        )
    )
    assert env.event_type == EventType.RESERVATION_CLAIMED


def test_envelope_rejects_malformed_reservation_claimed_payload() -> None:
    """Envelope rejects RESERVATION_CLAIMED with missing required fields."""
    with pytest.raises(ValidationError):
        Envelope.model_validate(
            _envelope_kwargs(
                EventType.RESERVATION_CLAIMED,
                {"cid": 42},  # missing venue_offer_id, size_usdt, signal_correlation_id, is_simulated
            )
        )


def test_envelope_validates_reservation_released_payload() -> None:
    """Envelope._validate_payload routes RESERVATION_RELEASED through ReservationReleasedPayload."""
    env = Envelope.model_validate(
        _envelope_kwargs(
            EventType.RESERVATION_RELEASED,
            {
                "cid": 42,
                "venue_offer_id": "paper_xyz",
                "size_usdt": 250.0,
                "reason": "venue_cancel",
                "signal_correlation_id": str(uuid4()),
                "is_simulated": True,
            },
        )
    )
    assert env.event_type == EventType.RESERVATION_RELEASED


def test_envelope_rejects_reservation_released_missing_reason() -> None:
    """Envelope rejects RESERVATION_RELEASED when reason field is missing."""
    with pytest.raises(ValidationError):
        Envelope.model_validate(
            _envelope_kwargs(
                EventType.RESERVATION_RELEASED,
                {
                    "cid": 42,
                    "venue_offer_id": "paper_xyz",
                    "size_usdt": 250.0,
                    "signal_correlation_id": str(uuid4()),
                    "is_simulated": True,
                },  # missing reason
            )
        )
