"""Phase 4.3 schema additions: new EventType values, HealthTarget.LEDGER."""
from __future__ import annotations

from bfx_funding_bot.core.telemetry import EventType, HealthTarget


def test_event_type_has_reservation_claimed() -> None:
    assert EventType.RESERVATION_CLAIMED.value == "reservation_claimed"


def test_event_type_has_reservation_released() -> None:
    assert EventType.RESERVATION_RELEASED.value == "reservation_released"


def test_health_target_has_ledger() -> None:
    assert HealthTarget.LEDGER.value == "ledger"
