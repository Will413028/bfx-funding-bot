from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.candles.schemas import FundingCandle


def test_funding_candle_from_bitfinex_array() -> None:
    """Bitfinex candle response is [mts, open, close, high, low, volume]."""
    raw = [1704067200000, 0.000123, 0.000125, 0.000130, 0.000120, 12345.678]
    candle = FundingCandle.from_bitfinex(
        raw, symbol="fUST", timeframe="1h", period_agg="p2"
    )

    assert candle.symbol == "fUST"
    assert candle.timeframe == "1h"
    assert candle.period_agg == "p2"
    assert candle.mts == 1704067200000
    assert candle.open == Decimal("0.000123")
    assert candle.close == Decimal("0.000125")
    assert candle.high == Decimal("0.000130")
    assert candle.low == Decimal("0.000120")
    assert candle.volume == Decimal("12345.678")


def test_funding_candle_decimal_precision() -> None:
    """Ensure floats are converted to Decimal without precision loss."""
    raw = [1704067200000, 0.0001234567890123, 0.0001, 0.0002, 0.00009, 100.0]
    candle = FundingCandle.from_bitfinex(
        raw, symbol="fUST", timeframe="1h", period_agg="p2"
    )
    assert str(candle.open).startswith("0.000123456789")


def test_funding_candle_timestamp_helper() -> None:
    raw = [1704067200000, 0.0, 0.0, 0.0, 0.0, 0.0]
    candle = FundingCandle.from_bitfinex(
        raw, symbol="fUST", timeframe="1h", period_agg="p2"
    )
    assert candle.timestamp() == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def test_funding_candle_rejects_wrong_array_length() -> None:
    raw = [1704067200000, 0.0, 0.0]  # too short
    with pytest.raises((ValidationError, ValueError)):
        FundingCandle.from_bitfinex(
            raw, symbol="fUST", timeframe="1h", period_agg="p2"
        )
