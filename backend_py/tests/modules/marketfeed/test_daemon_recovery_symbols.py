from bfx_funding_bot.modules.marketfeed.config import CellConfig, configured_symbols
from bfx_funding_bot.modules.marketfeed.schemas import StrategyName


def _cell(symbol: str, period_agg: str) -> CellConfig:
    return CellConfig(
        strategy=StrategyName.MEAN_REVERSION, symbol=symbol, period_agg=period_agg,
        timeframe="1h",
        params={"threshold_sigma": 1.0, "ratio_sigma": 1.0, "ema_span": 10},
        reference_amount_usdt=150.0, staleness_budget_hours=48,
    )


def test_configured_symbols_dedups_preserving_order():
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2"), _cell("fUSD", "p2")]
    assert configured_symbols(cells) == ["fUST", "fUSD"]


def test_configured_symbols_single_currency():
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    assert configured_symbols(cells) == ["fUST"]
