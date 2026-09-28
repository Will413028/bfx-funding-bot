from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bfx_funding_bot.apps.research import research_strategy
from bfx_funding_bot.modules.backtest.cell_derivation import (
    DerivedCell,
    NoDistinguishableComboError,
    derive_cell_params,
    select_winner,
)
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel

_LINEAR_CONFIG = BacktestConfig(fill_model="linear-baseline")
_UNUSED_LINEAR_MODEL = FillRateModel.from_rows([], artifact=None)
_MR_SPEC = research_strategy("MeanReversionStrategy")
_BASELINE = research_strategy("AlwaysMarketRateStrategy")


def _combo(
    span: int,
    ts: str,
    sigma: str,
    mean_active: str,
    ir: str,
    pct: str,
) -> tuple[dict, Decimal, Decimal, Decimal]:  # type: ignore[type-arg]
    params = {"ema_span": span, "threshold_sigma": Decimal(ts), "ratio_sigma": Decimal(sigma)}
    return (params, Decimal(mean_active), Decimal(ir), Decimal(pct))


def test_select_winner_picks_max_mean_active_among_distinguishable() -> None:
    combos = [
        _combo(168, "1.0", "0.99", "0.0", "0", "0"),      # inert -> filtered
        _combo(24, "0.5", "0.05", "0.07", "0.5", "0.85"),  # best distinguishable
        _combo(24, "1.0", "0.05", "0.03", "0.3", "0.7"),
    ]
    winner = select_winner(combos)
    assert winner["ema_span"] == 24
    assert winner["threshold_sigma"] == Decimal("0.5")


def test_select_winner_stable_tie_break_by_param_tuple() -> None:
    combos = [
        _combo(168, "1.5", "0.05", "0.05", "0.4", "0.7"),
        _combo(24, "0.5", "0.05", "0.05", "0.4", "0.7"),  # equal mean_active
    ]
    # tie -> smallest (ema_span, threshold_sigma) wins, deterministically
    assert select_winner(combos)["ema_span"] == 24


def test_select_winner_all_inert_raises() -> None:
    combos = [_combo(24, "0.5", "0.05", "0.0", "0", "0")]
    with pytest.raises(NoDistinguishableComboError):
        select_winner(combos)


def _synthetic_series() -> list[FundingCandle]:
    # 8 months of hourly candles. Rate dips to 5% of normal for 10h every 52h,
    # then recovers. dip_every=52, dip_len=10 chosen so dips align with the
    # ~48-hour lending cooldown cycle, giving the selective strategy consistent
    # opportunities to skip low-rate windows (IR ~2.3 in validation runs).
    start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
    out = []
    for i in range(24 * 30 * 8):
        base = 0.0004
        close = base * (0.05 if (i % 52) < 10 else 1.0)  # periodic deep dips
        out.append(FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="a30",
            mts=start + i * 3_600_000,
            close=str(close),
        ))
    return out


def test_derive_cell_params_is_deterministic_and_distinguishable() -> None:
    candles = _synthetic_series()
    d1 = derive_cell_params(candles, config=_LINEAR_CONFIG, fill_model=_UNUSED_LINEAR_MODEL, strategy_spec=_MR_SPEC, baseline=_BASELINE)
    d2 = derive_cell_params(candles, config=_LINEAR_CONFIG, fill_model=_UNUSED_LINEAR_MODEL, strategy_spec=_MR_SPEC, baseline=_BASELINE)
    assert (d1.ema_span, d1.threshold_sigma, d1.ratio_sigma) == (
        d2.ema_span,
        d2.threshold_sigma,
        d2.ratio_sigma,
    )
    assert d1.information_ratio != 0  # selected combo actually acts
    assert isinstance(d1, DerivedCell)
    # Lock in the winner for the documented regime: a grid/regime change that
    # shifts the winner should fail loudly here, not pass silently.
    assert d1.ema_span == 24
    assert d1.threshold_sigma == Decimal("0.5")
