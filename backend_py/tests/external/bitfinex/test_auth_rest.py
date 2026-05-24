from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    parse_active_funding_offers,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError


def _row(offer_id=12345, symbol="fUSD", mts=1_700_000_000_000, amount=-100.0,
         status="ACTIVE", rate=0.00031, period=2):
    # 21-element funding offer row; only documented indices are meaningful.
    row = [None] * 21
    row[0] = offer_id
    row[1] = symbol
    row[2] = mts
    row[4] = amount        # negative for an offer; we store abs
    row[5] = amount
    row[10] = status
    row[14] = rate
    row[15] = period
    return row


def test_parse_happy_path():
    offers = parse_active_funding_offers([_row()])
    assert offers == [
        ActiveFundingOffer(
            venue_offer_id="12345", symbol="fUSD", amount=Decimal("100.0"),
            rate=0.00031, period_days=2, mts_created=1_700_000_000_000,
            status="ACTIVE",
        )
    ]


def test_parse_empty():
    assert parse_active_funding_offers([]) == []


def test_parse_rejects_non_list():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_offers({"not": "a list"})


def test_parse_rejects_short_row():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_offers([[1, "fUSD", 0]])  # len < 16
