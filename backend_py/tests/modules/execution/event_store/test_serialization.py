from decimal import Decimal
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    event_type_of,
    serialize_event,
)
from bfx_funding_bot.modules.execution.events import (
    CreditClosed,
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationReleased,
)

_CID = 123
_VOI = "venue-1"
_SCID = UUID("11111111-1111-1111-1111-111111111111")


def _claimed() -> ReservationClaimed:
    return ReservationClaimed(cid=_CID, venue_offer_id=_VOI, size_usdt=Decimal("10.5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=7, occurred_at_ms=1000, symbol="fUST")


def test_event_type_of() -> None:
    assert event_type_of(_claimed()) == "RESERVATION_CLAIMED"


@pytest.mark.parametrize("event", [
    _claimed(),
    OrderFilled(cid=_CID, venue_offer_id=_VOI, credit_id="cr-1", size_usdt=Decimal("3.25"),
        fill_rate=0.0004, signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=8, occurred_at_ms=2000, symbol="fUST"),
    ReservationReleased(cid=_CID, venue_offer_id=_VOI, size_usdt=Decimal("2"),
        reason="missing_from_venue", signal_correlation_id=_SCID, account_id="acct",
        is_simulated=True, venue_seq=9, occurred_at_ms=3000, symbol="fUST"),
])
def test_roundtrip(event: object) -> None:
    etype = event_type_of(event)
    payload = serialize_event(event)
    assert isinstance(payload, dict)
    restored = deserialize_event(etype, payload)
    assert restored == event  # frozen dataclasses compare by value


def test_credit_closed_roundtrip() -> None:
    ev = CreditClosed(symbol="fUST", credit_id=123456, amount=Decimal("1338.02976177"),
        rate=0.000174, period_days=2, mts_create=1716383500000, account_id="acct",
        is_simulated=False, venue_seq=42, occurred_at_ms=1716385500000)
    assert event_type_of(ev) == "CREDIT_CLOSED"
    restored = deserialize_event("CREDIT_CLOSED", serialize_event(ev))
    assert restored == ev


def test_intent_failed_event_type_of() -> None:
    intent = ReservationIntent(cid=1, size_usdt=Decimal("5"), symbol="fUST",
        execution_decision_id="d-serialization", signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000)
    failed = ReservationFailed(cid=1, size_usdt=Decimal("5"), symbol="fUST",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        reason="submit_failed", occurred_at_ms=1000)
    assert event_type_of(intent) == "RESERVATION_INTENT"
    assert event_type_of(failed) == "RESERVATION_FAILED"


@pytest.mark.parametrize("event", [
    ReservationIntent(cid=9, size_usdt=Decimal("7.5"), symbol="fUST",
        execution_decision_id="d-serialization", signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000),
    ReservationFailed(cid=9, size_usdt=Decimal("7.5"), symbol="fUST",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=False,
        reason="submit_failed", occurred_at_ms=2000),
])
def test_intent_failed_roundtrip(event: object) -> None:
    etype = event_type_of(event)
    restored = deserialize_event(etype, serialize_event(event))
    assert restored == event


def test_intent_failed_legacy_payload_upcasts_symbol() -> None:
    """Pre-symbol event_log rows have no `symbol`; deserialize injects fUST."""
    legacy = {"cid": 9, "size_usdt": "7.5",
              "signal_correlation_id": str(_SCID), "account_id": "acct",
              "is_simulated": True, "occurred_at_ms": 1000}
    ev = deserialize_event("RESERVATION_INTENT", legacy)
    assert ev.symbol == "fUST"          # type: ignore[attr-defined]
    assert ev.amount == Decimal("7.5")  # type: ignore[attr-defined]
    assert ev.execution_decision_id is None  # type: ignore[attr-defined]
    assert ev.is_legacy_uncorrelated is True  # type: ignore[attr-defined]


def test_historical_intent_without_execution_decision_id_is_explicitly_legacy() -> None:
    payload = {
        "cid": 9,
        "size_usdt": "7.5",
        "symbol": "fUST",
        "signal_correlation_id": str(_SCID),
        "account_id": "acct",
        "is_simulated": True,
    }

    event = deserialize_event("RESERVATION_INTENT", payload)
    assert event.execution_decision_id is None  # type: ignore[attr-defined]
    assert event.reservation_ref is None  # type: ignore[attr-defined]
    assert event.is_legacy_uncorrelated is True  # type: ignore[attr-defined]


def test_decimal_preserved_as_string() -> None:
    payload = serialize_event(_claimed())
    assert payload["size_usdt"] == "10.5"  # Decimal serialized as str, not float


@pytest.mark.parametrize("etype,extra", [
    ("ORDER_FILL", {"venue_offer_id": "v1", "credit_id": None, "fill_rate": 0.0}),
    ("RESERVATION_CLAIMED", {"venue_offer_id": "v1"}),
    ("RESERVATION_RELEASED", {"venue_offer_id": "v1", "reason": "venue_cancel"}),
])
def test_legacy_position_event_without_symbol_upcasts_to_fust(
    etype: str, extra: dict[str, object],
) -> None:
    """The 4 position events gained a mandatory `symbol` in Phase 2; event_log
    rows written before that carry no `symbol`. deserialize must upcast them to
    fUST exactly like INTENT/FAILED — otherwise a manual rebuild_snapshot_from_log
    crashes at the offer_claims projection or silently drops the legacy fUST fills
    in the position_state tail fold."""
    legacy = {"cid": 9, "size_usdt": "12.5",
              "signal_correlation_id": str(_SCID), "account_id": "acct",
              "is_simulated": True, "occurred_at_ms": 1000, **extra}
    ev = deserialize_event(etype, legacy)
    assert ev.symbol == "fUST"           # type: ignore[attr-defined]
    assert ev.amount == Decimal("12.5")  # type: ignore[attr-defined]
