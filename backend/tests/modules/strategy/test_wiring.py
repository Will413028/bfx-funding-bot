"""Strategy wiring parity, boundary reconstruction, and sealed facade."""
from dataclasses import FrozenInstanceError, asdict
from decimal import Decimal
from typing import Any

import pytest

import bfx_funding_bot.modules.strategy as strategy_facade
from bfx_funding_bot.apps.research import research_strategy
from bfx_funding_bot.modules.candles.reindex import FilledCandle, reindex_and_ffill
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import FilledCandle as LegacyFilledCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill as legacy_reindex
from bfx_funding_bot.modules.marketfeed.divergence_reporter import ExtractedSignal
from bfx_funding_bot.modules.strategy import (
    CellConfig,
    Strategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.adaptive_period import (
    AdaptivePeriodStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.always_frr import AlwaysFrrStrategy
from bfx_funding_bot.modules.strategy._internal.strategies.always_market_rate import (
    AlwaysMarketRateStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.base import Strategy as StrategyABC
from bfx_funding_bot.modules.strategy._internal.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.mean_reversion_frr_floor import (
    MeanReversionFrrFloorStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.rate_percentile import (
    RatePercentileStrategy,
)
from bfx_funding_bot.modules.strategy.contracts import Strategy as InstanceProtocol
from bfx_funding_bot.modules.strategy.wiring import (
    RESEARCH_STRATEGIES,
    build_strategy,
    build_strategy_at_boundary,
)

D = Decimal
HOUR = 3_600_000
LIVE_PARAMS = [
    ("mean_reversion", {"ema_span": "3", "threshold_sigma": "1.500", "ratio_sigma": 0.0042}),
    ("rate_percentile", {"percentile": "75", "lookback_hours": "3"}),
    ("adaptive_period", {"ema_span": "3", "ratio_sigma": "0.004200", "t1": "0.50",
                         "t2": 2.0, "p_mid": "7", "p_long": "14"}),
]


def candle(hour: int, close: str | None = "0.0001234567890123456789012345") -> FundingCandle:
    return FundingCandle(symbol="fUSD", timeframe="1h", period_agg="a30",
                         mts=hour * HOUR, close=D(close) if close is not None else None)


def cell_for(name: str, params: dict[str, Any]) -> CellConfig:
    return CellConfig.model_validate({
        "strategy": name, "symbol": "fUSD", "period_agg": "a30", "params": params,
    })


def assert_diagnostics_match_properties(strategy: Any) -> None:
    snapshot = strategy.diagnostics()
    for key, value in asdict(snapshot).items():
        legacy = getattr(strategy, key)
        assert value == legacy
        assert type(value) is type(legacy)
        if isinstance(value, Decimal):
            assert value.as_tuple() == legacy.as_tuple()
        if isinstance(value, tuple):
            assert [v.as_tuple() for v in value] == [v.as_tuple() for v in legacy]


@pytest.mark.parametrize("name,params", LIVE_PARAMS)
def test_live_constructor_and_step_parity(name: str, params: dict[str, Any]) -> None:
    cell = cell_for(name, params)
    # Validation deliberately retains input strings/floats; factory must coerce.
    assert cell.params == params
    expected = {
        "mean_reversion": lambda: MeanReversionStrategy(
            ema_span=3, threshold_sigma=D("1.500"), ratio_sigma=D("0.0042"),
        ),
        "rate_percentile": lambda: RatePercentileStrategy(percentile=75, lookback_hours=3),
        "adaptive_period": lambda: AdaptivePeriodStrategy(
            ema_span=3, ratio_sigma=D("0.004200"), t1=D("0.50"), t2=D("2.0"),
            p_mid=7, p_long=14,
        ),
    }
    old, new = expected[name](), build_strategy(cell)
    assert old.name == new.name
    for c in [candle(0), candle(1, None), candle(2, "0"), candle(3, "0.002"),
              candle(4, "0.000001"), candle(5)]:
        assert ExtractedSignal.extract(cell, old, c) == ExtractedSignal.extract(cell, new, c)
        assert_diagnostics_match_properties(new)
        assert new.diagnostics() == old.diagnostics()


@pytest.mark.parametrize("name,params", LIVE_PARAMS)
@pytest.mark.parametrize("history,ref,budget,count", [
    ([], 4, 2, 0),
    ([candle(0)], 0, 2, 0),
    ([candle(0), candle(1), candle(2, "0.002")], 2, 2, 2),
    ([candle(0), candle(4, "0.002")], 4, 1, 2),
    ([candle(0)], 3, 2, 3),
    ([candle(0)], 4, 1, 2),
    ([candle(5)], 4, 2, 0),
    ([candle(4), candle(0), candle(2, None), candle(7)], 4, 1, 4),
])
def test_boundary_parity(
    name: str, params: dict[str, Any], history: list[FundingCandle],
    ref: int, budget: int, count: int,
) -> None:
    cell = cell_for(name, params)
    kwargs = {"cell": cell, "history": history, "ref_mts": ref * HOUR, "budget_hours": budget}
    new = build_strategy_at_boundary(**kwargs)
    old = build_strategy(cell)
    filled = reindex_and_ffill(history, ref_mts=ref * HOUR, max_gap_hours=budget)
    observed = [fc.candle for fc in filled[:-1] if fc.candle is not None]
    for candle_to_observe in observed:
        old.observe(candle_to_observe)
    assert new.observed_count == len(observed) == count
    assert new.strategy.name == old.name
    assert new.strategy.diagnostics() == old.diagnostics()
    assert_diagnostics_match_properties(new.strategy)
    pre_boundary_diagnostics = old.diagnostics()
    boundary = candle(ref, "0.00002")
    assert ExtractedSignal.extract(cell, new.strategy, boundary) == ExtractedSignal.extract(
        cell, old, boundary,
    )
    assert new.strategy.diagnostics() == old.diagnostics()
    fresh = build_strategy_at_boundary(**kwargs)
    assert fresh.strategy is not new.strategy
    assert fresh.strategy.diagnostics() == pre_boundary_diagnostics
    with pytest.raises(FrozenInstanceError):
        new.observed_count = 99


@pytest.mark.parametrize("name,params,error_type,error_args", [
    ("unknown", {}, ValueError, ("unsupported strategy 'unknown'",)),
    ("mean_reversion", {"ema_span": "bad"}, ValueError,
     ("invalid literal for int() with base 10: 'bad'",)),
    ("rate_percentile", {"percentile": "bad"}, ValueError,
     ("invalid literal for int() with base 10: 'bad'",)),
    ("adaptive_period", {"ema_span": "bad"}, ValueError,
     ("invalid literal for int() with base 10: 'bad'",)),
    ("mean_reversion", {}, KeyError, ("ema_span",)),
])
def test_constructor_errors_are_unchanged(
    name: str, params: dict[str, Any], error_type: type[Exception], error_args: tuple[str, ...],
) -> None:
    cell = CellConfig.model_construct(strategy=name, symbol="fUSD", period_agg="a30", params=params)
    with pytest.raises(error_type) as new:
        build_strategy(cell)
    assert new.value.args == error_args


RESEARCH_CASES = [
    (AdaptivePeriodStrategy, {"ema_span": 3, "ratio_sigma": D("0.05"), "t1": D("0.5"),
                              "t2": D("2"), "p_mid": 7, "p_long": 14}),
    (AlwaysFrrStrategy, {"period_days": 7}),
    (AlwaysMarketRateStrategy, {"period_days": 7}),
    (MeanReversionStrategy, {"ema_span": 3, "threshold_sigma": D("0.5"),
                             "ratio_sigma": D("0.05")}),
    (MeanReversionFrrFloorStrategy, {"ema_span": 3, "threshold_sigma": D("0.5"),
                                     "ratio_sigma": D("0.05")}),
    (RatePercentileStrategy, {"percentile": 75, "lookback_hours": 3}),
]


@pytest.mark.parametrize("cls,params", RESEARCH_CASES)
def test_research_catalog_parity(cls: type[StrategyABC], params: dict[str, Any]) -> None:
    spec = research_strategy(cls.__name__)
    eda = {"close_over_ema_sigma_24": D("0.0042000"),
           "close_over_ema_sigma_168": D("0.006700"), "acf_168h_pass": True}
    if cls in (AlwaysFrrStrategy, AlwaysMarketRateStrategy):
        assert spec.param_grid_for_cell("fUSD", "a30", eda) == [{}]
    else:
        assert spec.param_grid_for_cell("fUSD", "a30", eda) == cls.param_grid_for_cell(
            "fUSD", "a30", eda,
        )
    callback_calls: list[int] = []

    def frr_at(mts: int) -> Decimal | None:
        callback_calls.append(mts)
        return None if mts == 2 * HOUR else D("0.0002345678901234567890123456")

    kwargs = dict(params)
    if cls in (AlwaysFrrStrategy, MeanReversionFrrFloorStrategy):
        kwargs["frr_at"] = frr_at
    old, new = cls(**kwargs), spec.create(**kwargs)
    assert new.name == old.name
    assert spec.create(**kwargs) is not new
    for c in [candle(0, None), candle(1, "0.001"), candle(2, "0.00001"),
              candle(3, "0.000001"), candle(4, "0.002")]:
        old.observe(c)
        new.observe(c)
        calls_before = len(callback_calls)
        expected = old.decide(c)
        old_calls = callback_calls[calls_before:]
        calls_before = len(callback_calls)
        assert new.decide(c) == expected
        assert callback_calls[calls_before:] == old_calls
        assert new.diagnostics() == old.diagnostics()
        assert_diagnostics_match_properties(new)
    if cls in (AlwaysFrrStrategy, MeanReversionFrrFloorStrategy):
        assert 2 * HOUR in callback_calls  # missing FRR exercised
        assert 3 * HOUR in callback_calls  # FRR floor exercised


def test_sealed_facade_and_complete_catalog() -> None:
    assert Strategy is InstanceProtocol
    assert issubclass(MeanReversionStrategy, StrategyABC)
    assert FilledCandle is LegacyFilledCandle
    assert reindex_and_ffill is legacy_reindex
    assert {s.name for s in RESEARCH_STRATEGIES} == {cls.__name__ for cls, _ in RESEARCH_CASES}
    assert len(RESEARCH_STRATEGIES) == 6
    for cls, _ in RESEARCH_CASES:
        assert cls.__name__ not in vars(strategy_facade)
        assert cls.__name__ not in strategy_facade.__all__
