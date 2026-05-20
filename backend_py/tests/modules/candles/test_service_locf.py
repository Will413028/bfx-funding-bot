"""Unit tests for LOCF (Last Observation Carried Forward) primitive."""

from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import FilledCandle, reindex_and_ffill


def _make_candle(mts: int, close: str = "0.0001") -> FundingCandle:
    """Helper to construct a minimal FundingCandle for tests."""
    return FundingCandle(
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        mts=mts,
        open=Decimal(close),
        close=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        volume=Decimal("0"),
    )


HOUR_MS = 3_600_000


def test_locf_identity_on_dense_input() -> None:
    """100% dense hourly candles → no-op (Section Backtest identity proof)."""
    start_mts = 1_700_000_000_000
    n = 168
    candles = [_make_candle(start_mts + i * HOUR_MS, f"0.{i:04d}") for i in range(n)]
    ref_mts = candles[-1].mts

    filled = reindex_and_ffill(candles, ref_mts=ref_mts, max_gap_hours=12)

    assert len(filled) == n
    for original, wrapped in zip(candles, filled, strict=True):
        assert wrapped.candle == original
        assert wrapped.is_stale is False
        assert wrapped.stale_seconds == 0
