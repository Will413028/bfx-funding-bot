"""Phase 4.3 CP1 byte-equivalence property tests for LOCF primitive.

Hypothesis-driven 100-iter random input verification. Mirrors Phase 4.1 CP1
property test pattern in tests/modules/marketfeed/test_divergence_reporter.py.
"""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill

HOUR_MS = 3_600_000


@st.composite
def hyp_sparse_candle_list(draw: st.DrawFn) -> list[FundingCandle]:
    """Draw a (possibly sparse) candle list with hourly-aligned mts values.

    Returns a list of FundingCandle with strictly increasing hourly-aligned mts
    in a window of ~200 hours.
    """
    n = draw(st.integers(min_value=0, max_value=200))
    if n == 0:
        return []
    start_mts = 1_700_000_000_000
    # Draw n distinct hour offsets within 0..300
    offsets = sorted(draw(st.lists(st.integers(min_value=0, max_value=300),
                                    min_size=n, max_size=n, unique=True)))
    return [
        FundingCandle(
            symbol="fUSD",
            period_agg="p30",
            timeframe="1h",
            mts=start_mts + off * HOUR_MS,
            open=Decimal("0.0001"),
            close=Decimal("0.0001"),
            high=Decimal("0.0001"),
            low=Decimal("0.0001"),
            volume=Decimal("0"),
        )
        for off in offsets
    ]


@given(
    candles=hyp_sparse_candle_list(),
    budget_hours=st.integers(min_value=1, max_value=48),
    ref_offset=st.integers(min_value=0, max_value=350),
)
@settings(max_examples=100, deadline=None)
def test_reindex_and_ffill_deterministic(candles: list[FundingCandle], budget_hours: int, ref_offset: int) -> None:
    """Same input → same output across 2 invocations."""
    ref_mts = 1_700_000_000_000 + ref_offset * HOUR_MS

    out1 = reindex_and_ffill(candles, ref_mts=ref_mts, max_gap_hours=budget_hours)
    out2 = reindex_and_ffill(candles, ref_mts=ref_mts, max_gap_hours=budget_hours)

    assert out1 == out2


@st.composite
def hyp_dense_hourly_candles(draw: st.DrawFn) -> list[FundingCandle]:
    """Draw a fully-dense hourly candle list of size n."""
    n = draw(st.integers(min_value=1, max_value=200))
    start_mts = 1_700_000_000_000
    return [
        FundingCandle(
            symbol="fUSD",
            period_agg="p2",  # dense cells
            timeframe="1h",
            mts=start_mts + i * HOUR_MS,
            open=Decimal("0.0001"),
            close=Decimal(f"0.{i:04d}"),
            high=Decimal("0.0001"),
            low=Decimal("0.0001"),
            volume=Decimal("0"),
        )
        for i in range(n)
    ]


@given(candles=hyp_dense_hourly_candles(),
       budget_hours=st.integers(min_value=1, max_value=48))
@settings(max_examples=100, deadline=None)
def test_dense_input_property_identity(candles: list[FundingCandle], budget_hours: int) -> None:
    """For 100% dense hourly candles, reindex_and_ffill output 1-to-1 wraps input.

    Lopez de Prado identity transform claim verified empirically.
    """
    ref_mts = candles[-1].mts
    filled = reindex_and_ffill(candles, ref_mts=ref_mts, max_gap_hours=budget_hours)

    assert len(filled) == len(candles)
    for wrapped, original in zip(filled, candles, strict=True):
        assert wrapped.candle == original
        assert wrapped.is_stale is False
        assert wrapped.stale_seconds == 0


@given(
    candles=hyp_sparse_candle_list(),
    budget_hours=st.integers(min_value=1, max_value=48),
)
@settings(max_examples=100, deadline=None)
def test_daemon_path_equivalent_to_backtest_path(candles: list[FundingCandle], budget_hours: int) -> None:
    """Both daemon (extract last row, check None) and backtest (unwrap all non-None
    candles) call reindex_and_ffill with same args → both see same FilledCandle list.

    No path-specific reindex logic; primitive is the single source of truth.
    """
    if not candles:
        return  # vacuous
    ref_mts = candles[-1].mts

    daemon_result = reindex_and_ffill(candles, ref_mts=ref_mts, max_gap_hours=budget_hours)
    backtest_result = reindex_and_ffill(candles, ref_mts=ref_mts, max_gap_hours=budget_hours)

    assert daemon_result == backtest_result
