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


def test_locf_ffills_within_budget() -> None:
    """6h gap source-to-ref, budget=12h → 6 ffilled rows is_stale=True,
    stale_seconds linear from 1h to 6h."""
    start_mts = 1_700_000_000_000
    source = _make_candle(start_mts, "0.0042")
    ref_mts = start_mts + 6 * HOUR_MS

    filled = reindex_and_ffill([source], ref_mts=ref_mts, max_gap_hours=12)

    assert len(filled) == 7
    assert filled[0].candle == source
    assert filled[0].is_stale is False
    assert filled[0].stale_seconds == 0
    for i in range(1, 7):
        assert filled[i].candle == source, f"Slot {i} should ffill source candle"
        assert filled[i].is_stale is True
        assert filled[i].stale_seconds == i * 3600


def test_locf_caps_at_budget() -> None:
    """Source candle at T-24h, ref_mts=T, budget=12h.

    Slots T-24h (source itself) is_stale=False; slots T-23h to T-12h (12 slots)
    ffilled is_stale=True; slots T-11h to T-0 (12 slots) candle=None."""
    start_mts = 1_700_000_000_000
    source = _make_candle(start_mts, "0.0042")
    ref_mts = start_mts + 24 * HOUR_MS

    filled = reindex_and_ffill([source], ref_mts=ref_mts, max_gap_hours=12)

    assert len(filled) == 25
    assert filled[0].candle == source
    assert filled[0].is_stale is False
    for i in range(1, 13):
        assert filled[i].candle == source, f"Slot {i} should ffill within budget"
        assert filled[i].is_stale is True
        assert filled[i].stale_seconds == i * 3600
    for i in range(13, 25):
        assert filled[i].candle is None, f"Slot {i} should be hard-tier None"
        assert filled[i].is_stale is True
        assert filled[i].stale_seconds == i * 3600
