from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.external.bitfinex.funding_offer_row import (
    parse_funding_offer_row,
)

# Real funding-offer layout (matches fixtures/funding_offers_real.json rows):
#   [0]=id [1]=symbol [2]=mts_create [3]=mts_update [4]=amount(signed)
#   [10]=status [14]=rate [15]=period
_ROW = [998001, "fUSD", 1716595200000, 1716595260000, -120.5, -120.5,
        "LIMIT", None, None, 0, "ACTIVE", None, None, None, 0.00028, 2,
        0, 0, None, 0, None]


def test_parse_row_extracts_fields_at_correct_indices() -> None:
    r = parse_funding_offer_row(_ROW)
    assert r.venue_offer_id == "998001"
    assert r.symbol == "fUSD"
    assert r.mts_create == 1716595200000
    assert r.mts_update == 1716595260000
    assert r.amount == Decimal("120.5")  # abs of signed -120.5
    assert r.status == "ACTIVE"
    assert r.rate == 0.00028
    assert r.period_days == 2


def test_parse_row_tolerates_none_rate_and_period() -> None:
    row = list(_ROW)
    row[14] = None
    row[15] = None
    r = parse_funding_offer_row(row)
    assert r.rate is None
    assert r.period_days is None
    assert r.status == "ACTIVE"  # correctness fields still parsed


def test_parse_row_raises_on_short_row() -> None:
    with pytest.raises(BitfinexShapeError):
        parse_funding_offer_row([1, "fUSD", 2, 3])


def test_parse_row_raises_on_non_list() -> None:
    with pytest.raises(BitfinexShapeError):
        parse_funding_offer_row("not-a-list")  # type: ignore[arg-type]


def test_parse_row_length_boundary() -> None:
    """Lock the index boundary this SoT file exists to protect: index 15
    (period) is the highest accessed, so 16 is the minimum valid length."""
    with pytest.raises(BitfinexShapeError):
        parse_funding_offer_row(_ROW[:15])  # 15 elements → one short
    r = parse_funding_offer_row(_ROW[:16])  # exactly 16 → all fields present
    assert r.rate == 0.00028
    assert r.period_days == 2
