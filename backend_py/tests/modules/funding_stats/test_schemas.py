from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.funding_stats.schemas import FundingStat


def test_funding_stat_from_bitfinex_array() -> None:
    """Bitfinex /v2/funding/stats array shape (12 entries; verified by
    bfxapi/types/serializers.py + curl 2026-05-10):
    [MTS, _, _, FRR, AVG_PERIOD, _, _,
     FUNDING_AMOUNT, FUNDING_AMOUNT_USED, _, _,
     FUNDING_BELOW_THRESHOLD]
    """
    raw = [
        1715000000000,        # [0] MTS
        None, None,           # [1-2] placeholders
        5.8e-7,               # [3] FRR
        2.3,                  # [4] AVG_PERIOD
        None, None,           # [5-6] placeholders
        4.5e7,                # [7] FUNDING_AMOUNT
        2.1e7,                # [8] FUNDING_AMOUNT_USED
        None, None,           # [9-10] placeholders
        1.2e6,                # [11] FUNDING_BELOW_THRESHOLD
    ]
    fs = FundingStat.from_bitfinex(raw, symbol="fUSD")

    assert fs.symbol == "fUSD"
    assert fs.mts == 1715000000000
    assert fs.frr == Decimal("5.8e-7")
    assert fs.avg_period == Decimal("2.3")
    assert fs.funding_amount == Decimal("45000000.0")
    assert fs.funding_amount_used == Decimal("21000000.0")
    assert fs.funding_below_threshold == Decimal("1200000.0")


def test_funding_stat_decimal_precision() -> None:
    raw = [1715000000000, None, None, 5.823456789e-7, 2.3,
           None, None, 4.5e7, 2.1e7, None, None, 1.2e6]
    fs = FundingStat.from_bitfinex(raw, symbol="fUSD")
    assert fs.frr is not None
    assert str(fs.frr).startswith("5.823456789")  # no float drift


def test_funding_stat_handles_null_fields() -> None:
    """All fields nullable (real Bitfinex returns None for placeholders 1/2/5/6/9/10)."""
    raw = [1715000000000, None, None, None, None,
           None, None, None, None, None, None, None]
    fs = FundingStat.from_bitfinex(raw, symbol="fUSD")
    assert fs.frr is None
    assert fs.funding_amount is None


def test_funding_stat_timestamp_helper() -> None:
    raw = [1704067200000, None, None, 0.0, 0.0,
           None, None, 0.0, 0.0, None, None, 0.0]
    fs = FundingStat.from_bitfinex(raw, symbol="fUSD")
    assert fs.timestamp() == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def test_funding_stat_rejects_short_array() -> None:
    raw = [1715000000000, None, None, 5.8e-7]  # too short (4 < 12)
    with pytest.raises((ValidationError, ValueError)):
        FundingStat.from_bitfinex(raw, symbol="fUSD")
