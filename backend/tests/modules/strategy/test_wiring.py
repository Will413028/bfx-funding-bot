"""P3a parity: legacy consumers remain untouched until P3b."""
from dataclasses import FrozenInstanceError, asdict
from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.candles.reindex import FilledCandle, reindex_and_ffill
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import FilledCandle as LegacyFilledCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill as legacy_reindex
from bfx_funding_bot.modules.marketfeed.divergence_reporter import ExtractedSignal
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    build_strategy as legacy_build,
)
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    build_strategy_at_boundary as legacy_boundary,
)
from bfx_funding_bot.modules.strategy import (
    AdaptivePeriodStrategy,
    AlwaysFrrStrategy,
    AlwaysMarketRateStrategy,
    CellConfig,
    MeanReversionFrrFloorStrategy,
    MeanReversionStrategy,
    RatePercentileStrategy,
    Strategy,
    StrategyInstance,
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
    old, new = legacy_build(cell), build_strategy(cell)
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
    old, new = legacy_boundary(**kwargs), build_strategy_at_boundary(**kwargs)
    assert new.observed_count == old.observed_count == count
    assert new.strategy.name == old.strategy.name
    assert new.strategy.diagnostics() == old.strategy.diagnostics()
    assert_diagnostics_match_properties(new.strategy)
    boundary = candle(ref, "0.00002")
    assert ExtractedSignal.extract(cell, new.strategy, boundary) == ExtractedSignal.extract(
        cell, old.strategy, boundary,
    )
    assert new.strategy.diagnostics() == old.strategy.diagnostics()
    fresh = build_strategy_at_boundary(**kwargs)
    assert fresh.strategy is not new.strategy
    assert fresh.strategy.diagnostics() == legacy_boundary(**kwargs).strategy.diagnostics()
    with pytest.raises(FrozenInstanceError):
        new.observed_count = 99


@pytest.mark.parametrize("name,params", [
    ("unknown", {}),
    ("mean_reversion", {"ema_span": "bad"}),
    ("rate_percentile", {"percentile": "bad"}),
    ("adaptive_period", {"ema_span": "bad"}),
    ("mean_reversion", {}),
])
def test_constructor_errors_are_unchanged(name: str, params: dict[str, Any]) -> None:
    cell = CellConfig.model_construct(strategy=name, symbol="fUSD", period_agg="a30", params=params)
    with pytest.raises(Exception) as old:
        legacy_build(cell)
    with pytest.raises(type(old.value)) as new:
        build_strategy(cell)
    assert new.value.args == old.value.args


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
def test_research_catalog_parity(cls: type[Strategy], params: dict[str, Any]) -> None:
    spec = next(spec for spec in RESEARCH_STRATEGIES if spec.name == cls.__name__)
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


def test_legacy_exports_and_complete_catalog() -> None:
    assert StrategyInstance is InstanceProtocol
    assert Strategy is not InstanceProtocol
    assert issubclass(MeanReversionStrategy, Strategy)
    assert FilledCandle is LegacyFilledCandle
    assert reindex_and_ffill is legacy_reindex
    assert {s.name for s in RESEARCH_STRATEGIES} == {cls.__name__ for cls, _ in RESEARCH_CASES}
    assert len(RESEARCH_STRATEGIES) == 6
