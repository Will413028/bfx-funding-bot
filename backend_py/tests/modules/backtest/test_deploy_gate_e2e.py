"""Deploy sanity gate over the committed fixture + deployed canary config.

Marked `gate`: offline but slower than unit tests (rolling backtest over ~49
windows per cell). Run as its own CI job. Asserts the deployed config beats
passive AND that the pre-fix inert config (span=168/thr=1.0) would fail.
"""
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from bfx_funding_bot.modules.backtest.deploy_gate import GateResult, evaluate_gate
from bfx_funding_bot.modules.backtest.fixture_io import load_candles
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.oos_profitability import (
    active_return_summary,
    bootstrap_ci,
    paired_active_returns,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import MeanReversionStrategy
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import CellConfig, load_cells_only

pytestmark = pytest.mark.gate

ROOT = Path(__file__).resolve().parents[3]  # backend_py/
FIXTURES = ROOT / "fixtures" / "candles"
CANARY = ROOT / "configs" / "cells.canary.yaml"


def _load_canary_cells() -> list[CellConfig]:
    # Guard at collection time: pytest imports this module even for `-m "not
    # gate"`, so a missing config must not error the whole session. An empty
    # list yields no parametrized cases; test_canary_config_present (gate-only)
    # is what fails loudly if the deployed config is actually absent.
    return load_cells_only(CANARY) if CANARY.exists() else []


def _bootstrap_ci_mean(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    return bootstrap_ci(
        values, lambda vs: sum(vs, Decimal("0")) / Decimal(len(vs)), seed=20260528
    )


def _gate_for(
    make_strategy: Callable[[], Strategy],
    candles: list[FundingCandle],
) -> GateResult:
    windows = compute_wfo_windows(candles)
    strat_out, base_out = evaluate_oos_windows(candles, windows, make_strategy=make_strategy)
    active = active_return_summary(strat_out, base_out)
    actives = paired_active_returns(strat_out, base_out)
    ci_low, _ = _bootstrap_ci_mean(actives)
    return evaluate_gate(
        information_ratio=active.information_ratio,
        pct_outperform=active.pct_months_outperform,
        mean_active_ci_low=ci_low,
    )


def test_canary_config_present() -> None:
    # Closes the no-op risk: if the deployed config is missing/empty, the
    # parametrized gate above would silently run zero cases. Fail loudly here.
    assert CANARY.exists(), f"deployed canary config missing: {CANARY}"
    assert _load_canary_cells(), "canary config has no cells to gate"


@pytest.mark.parametrize("cell", _load_canary_cells())
def test_deployed_canary_cell_beats_passive(cell: CellConfig) -> None:
    candles = load_candles(FIXTURES / f"{cell.symbol}_{cell.period_agg}_{cell.timeframe}.jsonl.gz")
    p = cell.params
    result = _gate_for(
        lambda: MeanReversionStrategy(
            ema_span=int(p["ema_span"]),
            threshold_sigma=Decimal(str(p["threshold_sigma"])),
            ratio_sigma=Decimal(str(p["ratio_sigma"])),
        ),
        candles,
    )
    assert result.passed, f"{cell.symbol}_{cell.period_agg} failed deploy gate: {result.reasons}"


def test_old_inert_config_would_fail_gate() -> None:
    # The pre-fix hand-picked params for fUST_a30: span=168, thr=1.0, ratio=0.9915.
    candles = load_candles(FIXTURES / "fUST_a30_1h.jsonl.gz")
    result = _gate_for(
        lambda: MeanReversionStrategy(
            ema_span=168, threshold_sigma=Decimal("1.0"), ratio_sigma=Decimal("0.9915")
        ),
        candles,
    )
    assert result.passed is False
    assert result.distinguishable is False  # collapses to passive
