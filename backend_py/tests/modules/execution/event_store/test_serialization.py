from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    deserialize_stored_event,
    event_type_of,
    serialize_event,
    stored_event_identity,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
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
_EXCHANGE_ACCOUNT_ID = UUID("22222222-2222-2222-2222-222222222222")


def _ref(cid: int = _CID, voi: str | None = _VOI) -> ReservationRef:
    return ReservationRef(
        execution_decision_id=f"d-serialization-{cid}", cid=cid,
        signal_correlation_id=_SCID, venue_offer_id=voi,
    )


def _claimed() -> ReservationClaimed:
    return ReservationClaimed(cid=_CID, venue_offer_id=_VOI, size_usdt=Decimal("10.5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=7, occurred_at_ms=1000, symbol="fUST", reservation_ref=_ref())


def test_event_type_of() -> None:
    assert event_type_of(_claimed()) == "RESERVATION_CLAIMED"


@pytest.mark.parametrize("event", [
    _claimed(),
    OrderFilled(cid=_CID, venue_offer_id=_VOI, credit_id="cr-1", size_usdt=Decimal("3.25"),
        fill_rate=0.0004, signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=8, occurred_at_ms=2000, symbol="fUST", reservation_ref=_ref()),
    ReservationReleased(cid=_CID, venue_offer_id=_VOI, size_usdt=Decimal("2"),
        reason="missing_from_venue", signal_correlation_id=_SCID, account_id="acct",
        is_simulated=True, venue_seq=9, occurred_at_ms=3000, symbol="fUST", reservation_ref=_ref()),
])
def test_roundtrip(event: object) -> None:
    etype = event_type_of(event)
    payload = serialize_event(event)
    assert isinstance(payload, dict)
    assert payload["__schema_version__"] == 3
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
        reason="submit_failed", occurred_at_ms=1000, reservation_ref=_ref(1, None))
    assert event_type_of(intent) == "RESERVATION_INTENT"
    assert event_type_of(failed) == "RESERVATION_FAILED"


@pytest.mark.parametrize("event", [
    ReservationIntent(cid=9, size_usdt=Decimal("7.5"), symbol="fUST",
        execution_decision_id="d-serialization", signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000),
    ReservationFailed(cid=9, size_usdt=Decimal("7.5"), symbol="fUST",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=False,
        reason="submit_failed", occurred_at_ms=2000, reservation_ref=_ref(9, None)),
])
def test_intent_failed_roundtrip(event: object) -> None:
    etype = event_type_of(event)
    restored = deserialize_event(etype, serialize_event(event))
    assert restored == event


def test_public_deserializer_rejects_arbitrary_unversioned_payload() -> None:
    payload = {
        "cid": 9,
        "size_usdt": "7.5",
        "signal_correlation_id": str(_SCID),
        "account_id": "acct",
        "is_simulated": True,
        "occurred_at_ms": 1000,
    }

    with pytest.raises(ValueError, match="unversioned event payload"):
        deserialize_event("RESERVATION_INTENT", payload)


def test_public_deserializer_rejects_versioned_lifecycle_without_reservation_ref() -> None:
    payload = {
        "__schema_version__": 2,
        "__event_type__": "RESERVATION_INTENT",
        "cid": 9,
        "size_usdt": "7.5",
        "symbol": "fUST",
        "signal_correlation_id": str(_SCID),
        "account_id": "acct",
        "is_simulated": True,
    }

    with pytest.raises(TypeError, match=r"execution_decision_id|reservation_ref"):
        deserialize_event("RESERVATION_INTENT", payload)


def test_decimal_preserved_as_string() -> None:
    payload = serialize_event(_claimed())
    assert payload["size_usdt"] == "10.5"  # Decimal serialized as str, not float


def test_stored_event_uses_durable_exchange_account_identity() -> None:
    """Replay must trust the row owner, not a legacy payload realm string."""
    event = ReservationIntent(
        cid=10,
        size_usdt=Decimal("7.5"),
        symbol="fUST",
        execution_decision_id="d-serialization-10",
        signal_correlation_id=_SCID,
        account_id="legacy-realm",
        is_simulated=True,
        occurred_at_ms=1000,
    )
    row = EventLogRow(
        account_id="legacy-realm",
        exchange_account_id=_EXCHANGE_ACCOUNT_ID,
        deployment_environment="ci",
        event_type=event_type_of(event),
        cid=event.cid,
        venue_offer_id=None,
        venue_seq=None,
        payload=serialize_event(event),
        occurred_at_ms=event.occurred_at_ms,
    )

    restored = deserialize_stored_event(row)

    assert restored.account_id == str(_EXCHANGE_ACCOUNT_ID)  # type: ignore[attr-defined]


def test_v2_stored_identity_is_derived_without_mutating_payload() -> None:
    payload = {
        "cid": 19,
        "size_usdt": "7.5",
        "symbol": "fUST",
        "signal_correlation_id": str(_SCID),
        "account_id": "legacy-realm",
        "is_simulated": True,
        "execution_decision_id": "d-19",
        "reservation_ref": {
            "execution_decision_id": "d-19",
            "cid": 19,
            "signal_correlation_id": str(_SCID),
            "venue_offer_id": None,
        },
    }
    row = EventLogRow(
        event_seq=19,
        account_id="legacy-realm",
        deployment_environment="ci",
        event_type="RESERVATION_INTENT",
        cid=19,
        venue_offer_id=None,
        venue_seq=None,
        payload=payload,
        occurred_at_ms=1000,
        schema_version=2,
    )

    identity = stored_event_identity(row)

    assert identity.source == "derived_v2"
    assert identity.event_id.version == 5
    assert "__schema_version__" not in payload


@pytest.mark.asyncio
async def test_versioned_v2_stored_event_uses_historical_upcaster(
    sqlite_session: AsyncSession,
) -> None:
    bind = sqlite_session.bind
    assert bind is not None
    async with bind.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    payload = {
        "__event_type__": "RESERVATION_INTENT",
        "__schema_version__": 2,
        "cid": 20,
        "size_usdt": "7.5",
        "signal_correlation_id": str(_SCID),
        "account_id": "historical-realm",
        "is_simulated": True,
        "occurred_at_ms": 1000,
    }
    row = EventLogRow(
        account_id="historical-realm",
        deployment_environment="ci",
        event_type="RESERVATION_INTENT",
        cid=20,
        venue_offer_id=None,
        venue_seq=None,
        payload=payload,
        occurred_at_ms=1000,
        schema_version=2,
    )
    sqlite_session.add(row)
    await sqlite_session.flush()

    decoded = deserialize_stored_event(row)

    assert decoded.is_legacy_uncorrelated is True  # type: ignore[attr-defined]
    assert decoded.symbol == "fUST"  # type: ignore[attr-defined]
    assert decoded.event_id == stored_event_identity(row).event_id  # type: ignore[attr-defined]
    assert row.payload == payload


def test_stored_schema_version_mismatch_is_rejected() -> None:
    event = _claimed()
    row = EventLogRow(
        event_seq=20,
        account_id="acct",
        deployment_environment="ci",
        event_type=event_type_of(event),
        cid=event.cid,
        venue_offer_id=event.venue_offer_id,
        venue_seq=event.venue_seq,
        payload=serialize_event(event),
        occurred_at_ms=event.occurred_at_ms or 0,
        schema_version=2,
    )

    with pytest.raises(ValueError, match="schema_version"):
        stored_event_identity(row)


@pytest.mark.parametrize("etype,extra", [
    ("ORDER_FILL", {"venue_offer_id": "v1", "credit_id": None, "fill_rate": 0.0}),
    ("RESERVATION_CLAIMED", {"venue_offer_id": "v1"}),
    ("RESERVATION_RELEASED", {"venue_offer_id": "v1", "reason": "venue_cancel"}),
])
def test_public_deserializer_rejects_unversioned_position_event(
    etype: str, extra: dict[str, object],
) -> None:
    payload = {
        "cid": 9,
        "size_usdt": "12.5",
        "signal_correlation_id": str(_SCID),
        "account_id": "acct",
        "is_simulated": True,
        "occurred_at_ms": 1000,
        **extra,
    }

    with pytest.raises(ValueError, match="unversioned event payload"):
        deserialize_event(etype, payload)
