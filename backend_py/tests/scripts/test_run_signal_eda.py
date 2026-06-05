# tests/scripts/test_run_signal_eda.py
from decimal import Decimal

import numpy as np

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from scripts.run_signal_eda import _apply_fdr_and_decide, _Obs, run_funnel_for_cell

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


def test_run_funnel_obs_have_quintile_fields() -> None:
    """Each _Obs must carry quintile_spread (float) and quintile_monotonic (bool)."""
    candles, stats = _series(300)
    obs = run_funnel_for_cell("fUST_p2", candles, stats)
    assert obs, "expected at least one observation"
    for o in obs:
        assert hasattr(o, "quintile"), "missing quintile field"
        assert hasattr(o, "quintile_monotonic"), "missing quintile_monotonic field"
        assert isinstance(o.quintile, float), f"quintile should be float, got {type(o.quintile)}"
        assert isinstance(o.quintile_monotonic, bool), (
            f"quintile_monotonic should be bool, got {type(o.quintile_monotonic)}"
        )


def test_apply_fdr_and_decide_returns_verdicts_and_mask() -> None:
    """_apply_fdr_and_decide must return (verdicts, rejected_mask) with
    len(mask) == len(all_obs) and mask aligned to input order."""
    all_obs = [
        _Obs("frr_trend", "fUST_p2", "early", 7, 0.05, 0.001, 0.01, True),
        _Obs("frr_trend", "fUST_p2", "late",  7, 0.04, 0.002, 0.01, True),
        _Obs("spike_detect", "fUST_p2", "early", 7, 0.01, 0.9,  0.0,  False),
    ]
    verdicts, mask = _apply_fdr_and_decide(all_obs)
    assert isinstance(verdicts, list)
    assert isinstance(mask, list)
    assert len(mask) == len(all_obs), "mask must be aligned to all_obs"
    assert all(isinstance(m, bool) for m in mask), "mask elements must be bool"
    assert len(verdicts) == 2  # two distinct signal names


def test_run_funnel_handles_empty_gracefully() -> None:
    assert run_funnel_for_cell("fUST_p2", [], []) == []
