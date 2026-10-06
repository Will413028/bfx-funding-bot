"""An offer's status is its leading phrase, whatever narrative the venue appends.

Bitfinex reports offer state with narrative appended -- "EXECUTED at 0.0148%
(150.78)" is what it returned for the canary filled on 2026-09-23. Normalising
by lower-casing and replacing spaces turned that into the unknown status
"executed_at_0.0148%_(150.78)". The observations the live executor publishes in
``VenueSnapshotObserved`` normalise their status on construction.
"""
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)


@pytest.mark.parametrize("raw,state", [
    ("EXECUTED at 0.0148% (150.78)", "executed"),       # verbatim, 2026-09-23
    ("EXECUTED at 0.0178% (392.3)", "executed"),
    ("PARTIALLY FILLED at 0.02% (50.0)", "partially_filled"),
    ("CANCELED was: PARTIALLY FILLED at 0.02% (50.0)", "canceled"),
    ("EXECUTED @ 0.0148%(150.78)", "executed"),
    ("CANCELED", "canceled"),
    ("ACTIVE", "active"),
    ("  Active  ", "active"),
])
def test_the_state_is_the_leading_phrase(raw: str, state: str) -> None:
    offer = VenueOfferObservation(
        venue_offer_id="1", symbol="fUST", amount_original=Decimal("1"),
        amount_remaining=Decimal("0"), rate=None, period_days=2, status=raw,
        mts_created=1, mts_updated=2)
    credit = VenueCreditObservation(
        credit_id="2", symbol="fUST", amount=Decimal("1"), rate=None, period_days=2,
        status=raw)
    assert (offer.status, credit.status) == (state, state)
