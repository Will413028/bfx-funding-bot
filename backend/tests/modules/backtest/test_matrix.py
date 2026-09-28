from datetime import UTC, datetime
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.matrix import (
    CellVerdict,
    WindowOutcome,
    evaluate_cell_qualification,
    evaluate_strategy_qualification,
    pick_sweep_winner,
    run_cell_wfo,
)
from bfx_funding_bot.modules.backtest.schemas import BacktestResult
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy import MeanReversionStrategy, RatePercentileStrategy

_LINEAR_CONFIG = BacktestConfig(fill_model="linear-baseline")


def _result(sortino: str, net: str, fill: str = "1.0", n_trades: int = 100) -> BacktestResult:
    return BacktestResult(
        strategy_name="x", symbol="fUST", start_mts=0, end_mts=1, n_candles=0,
        gross_monthly_return_pct=Decimal("0"),
        net_monthly_return_pct=Decimal(net),
        max_drawdown_pct=Decimal("0"),
        n_trades=n_trades, fill_rate=Decimal(fill),
        sortino=Decimal(sortino),
    )


# ----- pick_sweep_winner -----


def test_pick_sweep_winner_picks_highest_sortino_when_finite() -> None:
    candidates = [
        ({"p": 1}, _result(sortino="0.5", net="0.3")),
        ({"p": 2}, _result(sortino="1.2", net="0.2")),
        ({"p": 3}, _result(sortino="0.9", net="0.4")),
    ]
    winner = pick_sweep_winner(candidates)
    assert winner is not None
    assert winner[0] == {"p": 2}


def test_pick_sweep_winner_ties_inf_breaks_on_raw_return() -> None:
    candidates = [
        ({"p": 1}, _result(sortino="Infinity", net="0.3")),
        ({"p": 2}, _result(sortino="Infinity", net="0.5")),
        ({"p": 3}, _result(sortino="2.5", net="0.8")),
    ]
    winner = pick_sweep_winner(candidates)
    assert winner is not None
    assert winner[0] == {"p": 2}


def test_pick_sweep_winner_filters_health_gates() -> None:
    candidates = [
        ({"p": 1}, _result(sortino="5.0", net="1.0", fill="0.2", n_trades=100)),
        ({"p": 2}, _result(sortino="3.0", net="0.5", fill="0.5", n_trades=5)),
        ({"p": 3}, _result(sortino="1.0", net="0.3", fill="0.5", n_trades=20)),
    ]
    winner = pick_sweep_winner(candidates)
    assert winner is not None
    assert winner[0] == {"p": 3}


def test_pick_sweep_winner_returns_none_when_all_filtered() -> None:
    candidates = [
        ({"p": 1}, _result(sortino="5.0", net="1.0", fill="0.2", n_trades=100)),
        ({"p": 2}, _result(sortino="3.0", net="0.5", fill="0.5", n_trades=5)),
    ]
    assert pick_sweep_winner(candidates) is None


# ----- evaluate_cell_qualification -----


def _outcome(
    idx: int, oos_net: str | None, baseline_net: str | None, fill: str = "1.0",
    n_trades: int = 100, status: str = "ok", incomplete_reason: str | None = None,
) -> WindowOutcome:
    return WindowOutcome(
        window_idx=idx,
        train_start_mts=idx * 1000, train_end_mts=idx * 1000 + 500,
        test_start_mts=idx * 1000 + 501, test_end_mts=idx * 1000 + 999,
        status=status,
        best_params={"p": 1} if status == "ok" else None,
        oos_net=Decimal(oos_net) if oos_net is not None else None,
        oos_max_dd=Decimal("0"),
        oos_fill_rate=Decimal(fill),
        oos_sortino=Decimal("1.0"),
        baseline_net=Decimal(baseline_net) if baseline_net is not None else None,
        baseline_sortino=Decimal("0.5"),
        incomplete_reason=incomplete_reason,
    )


def test_evaluate_cell_qualification_qualifies_when_60_pct_windows_beat_and_margin_met() -> None:
    outcomes = [_outcome(i, "0.50", "0.30") for i in range(7)]
    outcomes += [_outcome(i, "0.20", "0.30") for i in range(7, 10)]
    verdict = evaluate_cell_qualification(outcomes)
    assert verdict.qualifies is True
    assert verdict.windows_eligible == 10
    assert verdict.windows_strategy_beats_baseline == 7
    assert verdict.pct_windows_won == Decimal("0.7")


def test_evaluate_cell_qualification_fails_when_below_consistency_threshold() -> None:
    outcomes = [_outcome(i, "0.50", "0.30") for i in range(5)]
    outcomes += [_outcome(i, "0.20", "0.30") for i in range(5, 10)]
    verdict = evaluate_cell_qualification(outcomes)
    assert verdict.qualifies is False
    assert verdict.windows_strategy_beats_baseline == 5


def test_evaluate_cell_qualification_fails_when_margin_below_threshold() -> None:
    outcomes = [_outcome(i, "0.31", "0.30") for i in range(10)]
    verdict = evaluate_cell_qualification(outcomes)
    assert verdict.qualifies is False
    assert verdict.windows_strategy_beats_baseline == 10
    assert verdict.relative_margin < Decimal("0.05")


def test_evaluate_cell_qualification_excludes_skipped_windows_from_denominator() -> None:
    outcomes = [_outcome(i, "0.50", "0.30") for i in range(6)]
    outcomes += [_outcome(i, "0.20", "0.30") for i in range(6, 8)]
    outcomes += [
        _outcome(i, None, "0.30", status="skipped:no_valid_candidate") for i in range(8, 10)
    ]
    verdict = evaluate_cell_qualification(outcomes)
    assert verdict.windows_eligible == 8
    assert verdict.windows_strategy_beats_baseline == 6
    assert verdict.qualifies is True


def test_evaluate_cell_qualification_cannot_qualify_when_a_window_is_incomplete() -> None:
    outcomes = [_outcome(i, "0.50", "0.30") for i in range(10)]
    outcomes.append(
        _outcome(10, None, None, status="incomplete", incomplete_reason="fill_model_missing")
    )

    verdict = evaluate_cell_qualification(outcomes)

    assert verdict.qualifies is False
    assert verdict.incomplete_windows == 1


def test_evaluate_cell_qualification_fails_when_health_pct_below_threshold() -> None:
    healthy = [_outcome(i, "0.50", "0.30", fill="1.0", n_trades=50) for i in range(5)]
    unhealthy = [_outcome(i, "0.50", "0.30", fill="0.1", n_trades=2) for i in range(5, 10)]
    verdict = evaluate_cell_qualification(healthy + unhealthy)
    assert verdict.qualifies is False
    assert verdict.health_pct == Decimal("0.5")


def test_evaluate_cell_qualification_returns_zero_verdict_when_no_eligible_windows() -> None:
    outcomes = [
        _outcome(i, None, "0.30", status="skipped:no_valid_candidate") for i in range(5)
    ]
    verdict = evaluate_cell_qualification(outcomes)
    assert verdict.qualifies is False
    assert verdict.windows_eligible == 0


# ----- evaluate_strategy_qualification -----


def _cell_verdict(qualifies: bool) -> CellVerdict:
    return CellVerdict(
        qualifies=qualifies, windows_eligible=10,
        windows_strategy_beats_baseline=7 if qualifies else 3,
        pct_windows_won=Decimal("0.7") if qualifies else Decimal("0.3"),
        mean_strategy_net=Decimal("0.5"), mean_baseline_net=Decimal("0.3"),
        relative_margin=Decimal("0.66") if qualifies else Decimal("0"),
        health_pct=Decimal("0.95"),
        incomplete_windows=0,
    )


def test_evaluate_strategy_qualification_pass_when_4_of_6_cells_qualify() -> None:
    cells = [_cell_verdict(True) for _ in range(4)] + [_cell_verdict(False) for _ in range(2)]
    verdict = evaluate_strategy_qualification(cells)
    assert verdict.qualifies is True
    assert verdict.cells_qualifying == 4
    assert verdict.cells_played == 6


def test_evaluate_strategy_qualification_fail_when_3_of_6_cells_qualify() -> None:
    cells = [_cell_verdict(True) for _ in range(3)] + [_cell_verdict(False) for _ in range(3)]
    verdict = evaluate_strategy_qualification(cells)
    assert verdict.qualifies is False
    assert verdict.cells_qualifying == 3


# ----- run_cell_wfo (integration test on synthetic data) -----


def _synthetic_12_months_hourly() -> list[FundingCandle]:
    start = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=start + i * 3_600_000,
            open=Decimal("0.0001"), close=Decimal("0.0001"),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for i in range(12 * 30 * 24)
    ]


def test_run_cell_wfo_with_rate_percentile_produces_outcomes_per_window() -> None:
    candles = _synthetic_12_months_hourly()
    windows = compute_wfo_windows(candles, train_months=3, test_months=1, step_months=1)
    assert len(windows) >= 8

    eda_cell = {"acf_168h_pass": False}
    outcomes, baselines = run_cell_wfo(
        strategy_class=RatePercentileStrategy,
        candles=candles,
        eda_cell=eda_cell,
        cell_key="fUST_p2",
        wfo_windows=windows,
        config=_LINEAR_CONFIG,
        fill_model=None,
    )
    assert len(outcomes) == len(windows)
    assert len(baselines) == len(windows)
    for o in outcomes:
        assert o.status in ("ok", "skipped:no_valid_candidate", "incomplete")
        assert o.window_idx >= 0


def test_run_cell_wfo_with_mean_reversion_produces_outcomes_per_window() -> None:
    candles = _synthetic_12_months_hourly()
    windows = compute_wfo_windows(candles, train_months=3, test_months=1, step_months=1)
    eda_cell = {
        "close_over_ema_sigma_24": Decimal("0.05"),
        "close_over_ema_sigma_168": Decimal("0.10"),
    }
    outcomes, baselines = run_cell_wfo(
        strategy_class=MeanReversionStrategy,
        candles=candles,
        eda_cell=eda_cell,
        cell_key="fUST_p2",
        wfo_windows=windows,
        config=_LINEAR_CONFIG,
        fill_model=None,
    )
    assert len(outcomes) == len(windows)
    assert len(baselines) == len(windows)


def test_run_cell_wfo_handles_empty_param_grid_gracefully() -> None:
    """If a strategy's param_grid_for_cell returns [] (e.g. EDA drop), all
    windows should be marked skipped:no_valid_candidate, not errored.
    """
    class _StubEmptyGridStrategy(RatePercentileStrategy):
        @classmethod
        def param_grid_for_cell(cls, symbol, period_agg, eda):
            return []

    candles = _synthetic_12_months_hourly()
    windows = compute_wfo_windows(candles, train_months=3, test_months=1, step_months=1)
    outcomes, _ = run_cell_wfo(
        strategy_class=_StubEmptyGridStrategy,
        candles=candles, eda_cell={}, cell_key="fUST_p2",
        wfo_windows=windows,
        config=_LINEAR_CONFIG,
        fill_model=None,
    )
    for o in outcomes:
        assert o.status == "skipped:no_valid_candidate"
        assert o.best_params is None
        assert o.oos_net is None
