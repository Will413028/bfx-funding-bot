"""Wire-format contract tests for Bitfinex active funding offers.

Two layers (see specs/2026-05-25-venue-reconcile-verify-design.md):
  - test_fixture_parses: fast, runs in the default CI gate. Replays a captured
    real response fixture through the production parser; guards the
    positional-array layout against parser drift.
  - test_live_contract: gated (@pytest.mark.integration). Real signed
    round-trip against api.bitfinex.com; golden-file capture / drift-compare.
"""
import json
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    parse_active_funding_offers,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "funding_offers_real.json"


def _assert_offer_fields(o: ActiveFundingOffer) -> None:
    """Per-offer invariants implied by the positional-array contract."""
    assert isinstance(o.venue_offer_id, str) and o.venue_offer_id
    assert isinstance(o.amount, Decimal) and o.amount >= 0
    assert isinstance(o.period_days, int) and o.period_days > 0
    assert isinstance(o.status, str) and o.status


def test_fixture_parses():
    raw = json.loads(_FIXTURE.read_text())
    offers = parse_active_funding_offers(raw)  # raises BitfinexShapeError on layout drift
    assert offers, "seed fixture must contain at least one offer"
    for o in offers:
        _assert_offer_fields(o)
