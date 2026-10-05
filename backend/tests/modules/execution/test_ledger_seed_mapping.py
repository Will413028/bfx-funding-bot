"""The legacy -> ledger vocabulary of the closure reader (pure parts).

The attribution table is total over what legacy ``_attribute_credits`` writes and refuses
anything else; a credit or group the ledger cannot key by (symbol, period, opening) refuses.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.ledger_seed import (
    LEGACY_ATTRIBUTION,
    claim_terms,
    credit_identity,
    map_attribution,
    seed_credit,
    seed_group,
    seed_offer,
)
from bfx_funding_bot.modules.ledger import ATTRIBUTION_BASES, SeedRefused

# Every value legacy ``CapitalRepository._attribute_credits`` can store as ``basis``.
LEGACY_BASES = {"funding_trade", "funding_trade_partial", "carried", "recent_fill", "none"}


def test_the_attribution_table_is_exactly_the_legacy_vocabulary() -> None:
    assert set(LEGACY_ATTRIBUTION) == LEGACY_BASES
    assert set(LEGACY_ATTRIBUTION.values()) <= ATTRIBUTION_BASES
    assert dict(LEGACY_ATTRIBUTION) == {
        "funding_trade": "trade",
        "funding_trade_partial": "trade",
        "carried": "carry",
        "recent_fill": "recent_fill",
        "none": "unattributed",
    }


@pytest.mark.parametrize("value", ["", "unattributed", "trade", "carry", None, 1, "NONE"])
def test_anything_else_is_refused_not_defaulted(value: object) -> None:
    with pytest.raises(SeedRefused) as raised:
        map_attribution(value)
    assert raised.value.reason == "attribution_basis_unknown"


def test_a_loan_id_maps_to_the_loan_of_that_venue_id() -> None:
    assert credit_identity("loan:42") == ("loan", "42")
    assert credit_identity("42") == ("credit", "42")
    with pytest.raises(SeedRefused):
        credit_identity("loan:")


def _credit(**changes: object) -> VenueCreditObservation:
    values: dict[str, object] = {
        "credit_id": "loan:7", "symbol": "fUST", "amount": Decimal("40"),
        "rate": Decimal("0.0001"), "period_days": 2, "status": "ACTIVE",
        "mts_created": 10, "mts_updated": 11, "mts_opening": 9,
    }
    values.update(changes)
    return VenueCreditObservation(**values)  # type: ignore[arg-type]


def test_a_credit_keeps_its_venue_opening_and_refuses_without_one() -> None:
    seeded = seed_credit(_credit())
    assert (seeded.source_kind, seeded.venue_credit_id, seeded.mts_opening, seeded.period_days) == (
        "loan", "7", 9, 2)
    for change in ({"mts_opening": None}, {"period_days": None}):
        with pytest.raises(SeedRefused) as raised:
            seed_credit(_credit(**change))
        assert raised.value.reason == "credit_opening_unkeyable"


def test_a_group_without_an_opening_is_refused() -> None:
    entry = {"symbol": "fUST", "amount": "40", "period": 2, "opening": 9,
             "cells": ["a", "b"], "basis": "recent_fill"}
    group = seed_group("loan:7", entry)
    assert (group.attribution_basis, group.cells) == ("recent_fill", frozenset({"a", "b"}))
    with pytest.raises(SeedRefused) as raised:
        seed_group("c-1", {**entry, "opening": None})  # a legacy by-id carry entry
    assert raised.value.reason == "credit_opening_unkeyable"


def test_an_offer_keeps_the_legacy_terms() -> None:
    offer = seed_offer(VenueOfferObservation(
        "o-1", "fUST", Decimal("300"), Decimal("200"), None, 2, "PARTIALLY FILLED", 5, 6,
        flags={"hidden": False},
    ))
    assert (offer.status, offer.rate, offer.flags) == ("partially_filled", None, {"hidden": False})


def test_a_claim_only_offer_takes_its_decision_terms_checked_against_the_offer() -> None:
    """R1-1: rate/period from the claim's decision (what legacy cancels by), equal to the
    observed offer's; otherwise the seed refuses (venue offer terms are immutable)."""
    offer = seed_offer(VenueOfferObservation(
        "o-1", "fUST", Decimal("120"), Decimal("120"), Decimal("0.0001"), 2, "ACTIVE", 5, 6,
        offer_type="LIMIT"))
    assert claim_terms(offer, size=Decimal("120"), rate=Decimal("0.00010"), period=2) == {
        "symbol": "fUST", "amount": "120", "rate": "0.00010", "period": 2, "type": "LIMIT"}
    for size, rate, period in ((Decimal("120"), Decimal("0.0002"), 2),
                               (Decimal("120"), Decimal("0.0001"), 3),
                               (Decimal("120"), None, 2), (Decimal("120"), Decimal("0.0001"), None),
                               (Decimal("121"), Decimal("0.0001"), 2)):
        with pytest.raises(SeedRefused) as raised:
            claim_terms(offer, size=size, rate=rate, period=period)
        assert raised.value.reason == "claim_offer_terms_mismatch"
