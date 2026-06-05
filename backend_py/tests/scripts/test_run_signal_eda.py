# tests/scripts/test_run_signal_eda.py
from decimal import Decimal

import numpy as np

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from scripts.run_signal_eda import run_funnel_for_cell

_DAY = 86_400_000


def _series(n: int):
    rng = np.random.default_rng(0)
    base = 1_500_000_000_000
    frrs = rng.normal(2e-6, 5e-7, size=n).clip(min=1e-7)
    closes = frrs * 1e2  # close correlated to frr -> non-trivial IC
    candles = [
        FundingCandle(symbol="fUST", timeframe="1D", period_agg="p2",
                      mts=base + i * _DAY, open=Decimal(str(closes[i])), high=Decimal(str(closes[i])),
                      low=Decimal(str(closes[i])), close=Decimal(str(closes[i])))
        for i in range(n)
    ]
    stats = [
        FundingStat(symbol="fUST", mts=base + i * _DAY, frr=Decimal(str(frrs[i])),
                    funding_amount=Decimal("1000"), funding_amount_used=Decimal(str(400 + i % 50)))
        for i in range(n)
    ]
    return candles, stats


def test_run_funnel_for_cell_returns_obs_per_signal() -> None:
    candles, stats = _series(300)
    obs = run_funnel_for_cell("fUST_p2", candles, stats)
    assert {o.signal_name for o in obs} == {"frr_trend", "spike_detect", "funding_supply", "utilization"}
    regimes = {o.regime for o in obs}
    assert regimes and regimes.issubset({"early", "late"})  # synthetic 2017 data -> all 'early'
    assert all(hasattr(o, "ic") and hasattr(o, "p_value") for o in obs)


def test_run_funnel_handles_empty_gracefully() -> None:
    assert run_funnel_for_cell("fUST_p2", [], []) == []
