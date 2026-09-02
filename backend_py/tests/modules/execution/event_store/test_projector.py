from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.projector import (
    InvalidVenueOfferTransition,
    VenueOfferState,
    apply_offer_transition,
    derive_v2_event_id,
    gross_exposure,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.tables import (
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.events import ReservationClaimed

_ACCOUNT = "550e8400-e29b-41d4-a716-446655440000"
_SCID = UUID("11111111-1111-1111-1111-111111111111")
_EVENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _claimed(*, event_id: UUID = _EVENT_ID) -> ReservationClaimed:
    return ReservationClaimed(
        cid=7,
        venue_offer_id="offer-7",
        amount=Decimal("12.50"),
        signal_correlation_id=_SCID,
        account_id=_ACCOUNT,
        is_simulated=False,
        venue_seq=10,
        occurred_at_ms=1_700_000_000_000,
        symbol="fUST",
        event_id=event_id,
        reservation_ref=ReservationRef(
            execution_decision_id="decision-7",
            cid=7,
            signal_correlation_id=_SCID,
            venue_offer_id="offer-7",
        ),
    )


def test_v3_event_identity_is_stable_and_serialized_lowercase() -> None:
    event = _claimed()

    assert event.event_id == _EVENT_ID
    payload = serialize_event(event)

    assert payload["__schema_version__"] == 3
    assert payload["event_id"] == str(_EVENT_ID)
    restored = deserialize_event("RESERVATION_CLAIMED", payload)
    assert restored == event


def test_v3_deserializer_rejects_missing_event_id() -> None:
    payload = serialize_event(_claimed())
    del payload["event_id"]

    with pytest.raises(ValueError, match="event_id"):
        deserialize_event("RESERVATION_CLAIMED", payload)


def test_v2_event_id_derivation_is_deterministic_and_payload_sensitive() -> None:
    first = derive_v2_event_id(
        event_seq=12,
        account_id="legacy-realm",
        deployment_environment="ci",
        event_type="RESERVATION_CLAIMED",
        occurred_at_ms=1000,
        payload={"cid": 7, "amount": "12.50"},
    )
    same = derive_v2_event_id(
        event_seq=12,
        account_id="legacy-realm",
        deployment_environment="ci",
        event_type="RESERVATION_CLAIMED",
        occurred_at_ms=1000,
        payload={"amount": "12.50", "cid": 7},
    )
    changed = derive_v2_event_id(
        event_seq=12,
        account_id="legacy-realm",
        deployment_environment="ci",
        event_type="RESERVATION_CLAIMED",
        occurred_at_ms=1000,
        payload={"cid": 8, "amount": "12.50"},
    )

    assert first == same
    assert first != changed
    assert isinstance(first, UUID)


def test_entity_rows_use_account_environment_venue_identity() -> None:
    offer_pk = {
        column.name for column in VenueOfferStateRow.__table__.primary_key.columns
    }
    credit_pk = {
        column.name for column in VenueCreditStateRow.__table__.primary_key.columns
    }

    assert offer_pk == {
        "exchange_account_id",
        "deployment_environment",
        "venue_offer_id",
    }
    assert credit_pk == {
        "exchange_account_id",
        "deployment_environment",
        "credit_id",
    }


def test_terminal_offer_cannot_reopen() -> None:
    terminal = VenueOfferState(
        venue_offer_id="offer-7",
        symbol="fUST",
        amount_original=Decimal("12.50"),
        amount_remaining=Decimal("0"),
        rate=Decimal("0.0002"),
        period_days=2,
        status="filled",
        mts_created=1000,
        mts_updated=2000,
        first_seen_event_seq=4,
        last_seen_event_seq=8,
    )

    with pytest.raises(InvalidVenueOfferTransition, match="terminal"):
        apply_offer_transition(terminal, status="active", event_seq=9, mts_updated=3000)


def test_gross_exposure_uses_decimal_buckets() -> None:
    assert gross_exposure(
        offered_amount=Decimal("10.10"),
        lent_amount=Decimal("20.20"),
        uncertain_amount=Decimal("3.30"),
    ) == Decimal("33.60")
