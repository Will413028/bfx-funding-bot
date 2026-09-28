from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy import AdaptivePeriodStrategy


def _c(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def _ap(ema_span: int = 24, t1: str = "0.5", t2: str = "1.5",
        ratio_sigma: str = "0.05", p_mid: int = 7, p_long: int = 30) -> AdaptivePeriodStrategy:
    return AdaptivePeriodStrategy(
        ema_span=ema_span, ratio_sigma=Decimal(ratio_sigma),
        t1=Decimal(t1), t2=Decimal(t2), p_mid=p_mid, p_long=p_long,
    )


def test_name_includes_params() -> None:
    assert _ap().name == "adaptive_period_ema24_t0.5_1.5"


def test_ema_current_none_before_first_observe() -> None:
    assert _ap().ema_current is None


def test_observe_seeds_then_tracks_ema() -> None:
    s = _ap()
    s.observe(_c(0, "0.0003"))
    assert s.ema_current == Decimal("0.0003")  # first observe seeds
    s.observe(_c(3600_000, "0.0005"))
    alpha = Decimal(2) / Decimal(24 + 1)
    expected = alpha * Decimal("0.0005") + (Decimal("1") - alpha) * Decimal("0.0003")
    assert s.ema_current == expected


def test_observe_skips_none_close_and_does_not_count_sample() -> None:
    s = _ap()
    s.observe(_c(0, "0.0003"))
    none_candle = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1,
        open=None, close=None, high=None, low=None, volume=None,
    )
    s.observe(none_candle)
    assert s.samples == 1
    assert s.ema_current == Decimal("0.0003")


def test_window_filled_after_ema_span_samples() -> None:
    s = _ap(ema_span=3)
    assert not s.window_filled
    for i in range(3):
        s.observe(_c(i, "0.0003"))
    assert s.window_filled


def _warm(s: AdaptivePeriodStrategy, level: str, n: int = 30) -> None:
    """Warm EMA to `level` with n samples so window_filled and ema≈level."""
    for i in range(n):
        s.observe(_c(i, level))


def test_decide_returns_none_when_close_is_none() -> None:
    s = _ap()
    none_candle = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=5,
        open=None, close=None, high=None, low=None, volume=None,
    )
    assert s.decide(none_candle) is None


def test_decide_period_floor_during_warmup() -> None:
    s = _ap(ema_span=24)
    s.observe(_c(0, "0.0001"))          # 1 sample << ema_span -> not warmed
    cand = _c(1, "0.0005")              # big positive deviation, but warmup
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None
    assert d.period_days == 2
    assert d.rate == Decimal("0.0005")  # always lends at market


def test_decide_floor_when_at_or_below_trend() -> None:
    # warmed flat at 0.0010, then a candle equal to EMA -> deviation 0 <= band1
    s = _ap(ema_span=3, t1="0.5", t2="1.5", ratio_sigma="0.10")
    _warm(s, "0.0010", n=5)
    cand = _c(100, "0.0010")
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None and d.period_days == 2


def test_decide_p_mid_when_mildly_above_trend() -> None:
    # ema_span=1 -> alpha=1.0 -> ema == previous close exactly (easy goldens).
    # band1 = 0.5*0.10 = 0.05 ; band2 = 1.5*0.10 = 0.15
    # want deviation in (0.05, 0.15]: close=0.0011 over ema=0.0010 -> dev=0.10
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=30)
    s.observe(_c(0, "0.0010"))          # ema=0.0010 (seed), samples=1>=ema_span=1
    cand = _c(1, "0.0011")
    # do NOT observe cand before decide, so ema stays 0.0010 for a clean golden
    d = s.decide(cand)
    assert d is not None and d.period_days == 7


def test_decide_p_long_on_spike() -> None:
    # deviation = 0.20 > band2 (0.15) -> p_long
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=30)
    s.observe(_c(0, "0.0010"))
    cand = _c(1, "0.0012")              # dev = 0.20
    d = s.decide(cand)
    assert d is not None and d.period_days == 30


def test_decide_boundary_band1_inclusive_to_floor() -> None:
    # deviation exactly == band1 (0.05) -> floor (<=)
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10")
    s.observe(_c(0, "0.0010"))
    cand = _c(1, "0.00105")             # dev = 0.05 == band1
    d = s.decide(cand)
    assert d is not None and d.period_days == 2


def test_decide_boundary_band2_inclusive_to_mid() -> None:
    # deviation exactly == band2 (0.15) -> p_mid (<=)
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7)
    s.observe(_c(0, "0.0010"))
    cand = _c(1, "0.00115")             # dev = 0.15 == band2
    d = s.decide(cand)
    assert d is not None and d.period_days == 7


def test_decide_clamps_p_long_above_max() -> None:
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=999)
    s.observe(_c(0, "0.0010"))
    cand = _c(1, "0.0012")              # spike tier
    d = s.decide(cand)
    assert d is not None and d.period_days == 120  # clamped to Bitfinex max


def test_decide_sets_last_period() -> None:
    s = _ap(ema_span=1, ratio_sigma="0.10")
    s.observe(_c(0, "0.0010"))
    s.decide(_c(1, "0.0012"))
    assert s.last_period == 30


def test_decide_rate_always_equals_close() -> None:
    s = _ap(ema_span=1, ratio_sigma="0.10")
    s.observe(_c(0, "0.0010"))
    for close in ("0.0009", "0.0011", "0.0012"):
        cand = _c(2, close)
        d = s.decide(cand)
        assert d is not None and d.rate == Decimal(close)


# ---------------------------------------------------------------------------
# Integration tests: engine order (observe THEN decide on the SAME candle)
# ---------------------------------------------------------------------------

def test_integrated_spike_emits_p_long_under_engine_order() -> None:
    # Engine calls observe(candle) then decide(candle) for the SAME candle.
    # ema_span=24 (alpha=2/25); warm flat at 0.0010 so ema=0.0010, then a +20%
    # candle. After observe: ema≈0.001016, deviation≈0.181 > band2 (0.15) -> p_long.
    s = _ap(ema_span=24, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=30)
    _warm(s, "0.0010", n=24)
    spike = _c(100, "0.0012")
    s.observe(spike)   # engine order: observe first
    d = s.decide(spike)
    assert d is not None and d.period_days == 30


def test_integrated_mid_emits_p_mid_under_engine_order() -> None:
    # ema_span=24 (alpha=2/25); warm flat at 0.0010 so ema=0.0010, then close=0.00106.
    # After observe: ema=0.0010048, deviation≈0.05494 in (band1=0.05, band2=0.15] -> p_mid.
    s = _ap(ema_span=24, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=30)
    _warm(s, "0.0010", n=24)
    mid = _c(100, "0.00106")
    s.observe(mid)   # engine order: observe first
    d = s.decide(mid)
    assert d is not None and d.period_days == 7


def test_integrated_flat_emits_floor_under_engine_order() -> None:
    # ema_span=24; warm flat at 0.0010 so ema=0.0010, then another 0.0010 candle.
    # After observe: ema=0.0010, deviation=0 <= band1 (0.05) -> floor (period_days=2).
    s = _ap(ema_span=24, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=30)
    _warm(s, "0.0010", n=24)
    flat = _c(100, "0.0010")
    s.observe(flat)   # engine order: observe first
    d = s.decide(flat)
    assert d is not None and d.period_days == 2


def test_param_grid_for_cell_from_eda() -> None:
    # 2026-06-04 band sweep + 2026-06-03 p_long sweep: single deployed candidate.
    grid = AdaptivePeriodStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={
            "close_over_ema_sigma_24": Decimal("0.05"),
            "close_over_ema_sigma_168": Decimal("0.08"),
        },
    )
    assert len(grid) == 1  # single deployed candidate (span-24, band (0.5,2.0), p_long=14)
    p = grid[0]
    assert p["ema_span"] == 24
    assert (p["t1"], p["t2"]) == (Decimal("0.5"), Decimal("2.0"))
    assert p["p_mid"] == 7
    assert p["p_long"] == 14
    assert p["ratio_sigma"] == Decimal("0.05")  # close_over_ema_sigma_24


def test_param_grid_for_cell_guard_deployed_candidate() -> None:
    # Guard: fails loudly if grid silently reverts to old exploration values.
    # Mirrors test_p14_config_present idiom: protects the deploy decision from
    # accidental reversion during refactoring.
    grid = AdaptivePeriodStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="a30",
        eda={"close_over_ema_sigma_24": Decimal("0.042")},
    )
    assert grid, "param_grid_for_cell must return at least one entry"
    p_longs = {p["p_long"] for p in grid}
    assert p_longs == {14}, f"all p_long must be 14 (deploy decision); got {p_longs}"
    ema_spans = {p["ema_span"] for p in grid}
    assert ema_spans == {24}, f"all ema_span must be 24 (sweep decision); got {ema_spans}"
    bands = {(p["t1"], p["t2"]) for p in grid}
    assert bands == {(Decimal("0.5"), Decimal("2.0"))}, (
        f"band must be {{(0.5, 2.0)}} (sweep decision); got {bands}"
    )


def test_build_strategy_from_cellconfig() -> None:
    from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy
    from bfx_funding_bot.modules.strategy import CellConfig

    cell = CellConfig(
        strategy="adaptive_period", symbol="fUST", period_agg="a30",
        params={"ema_span": 24, "ratio_sigma": 0.42, "t1": 0.5, "t2": 1.5,
                "p_mid": 7, "p_long": 30},
    )
    s = build_strategy(cell)
    assert isinstance(s, AdaptivePeriodStrategy)
    assert s.name == "adaptive_period_ema24_t0.5_1.5"


def test_build_strategy_is_deterministic() -> None:
    from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy
    from bfx_funding_bot.modules.strategy import CellConfig

    cell = CellConfig(
        strategy="adaptive_period", symbol="fUST", period_agg="a30",
        params={"ema_span": 1, "ratio_sigma": 0.10, "t1": 0.5, "t2": 1.5,
                "p_mid": 7, "p_long": 30},
    )
    a, b = build_strategy(cell), build_strategy(cell)
    a.observe(_c(0, "0.0010"))
    b.observe(_c(0, "0.0010"))
    cand = _c(1, "0.0012")
    da, db = a.decide(cand), b.decide(cand)
    assert da is not None and db is not None
    assert da.period_days == db.period_days == 30
    assert da.rate == db.rate


def test_experimental_cells_yaml_loads_and_builds() -> None:
    from pathlib import Path

    from bfx_funding_bot.apps.config import load_cells_only
    from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy

    cells = load_cells_only(Path("configs/cells.experimental.yaml"))
    assert len(cells) == 4
    assert all(c.strategy.value == "adaptive_period" for c in cells)
    assert {c.cell_id for c in cells} == {"fUST_a30", "fUST_p2", "fUSD_a30", "fUSD_p2"}
    for c in cells:
        s = build_strategy(c)
        assert s.name.startswith("adaptive_period_")
