"""Decision-space search on the trade-tape replay: both bounds, paired verdicts, trials."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bfx_funding_bot.apps.research import research_strategy
from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.decision_search import (
    VERDICT_NOT_SCORED,
    VERDICT_POSITIVE,
    VERDICT_WAIT_FOR_LIVE,
    QuoteOffset,
    SearchArm,
    SearchRun,
    build_search_report,
    evaluate_search_arms,
    render_search_markdown,
    search_report_to_json,
    tape_coverage,
    windows_within_tape,
)
from bfx_funding_bot.modules.backtest.period_structure import ArmSpec
from bfx_funding_bot.modules.backtest.replay import TapeDays
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from tests.modules.backtest.test_replay import write_day

_HOUR = 3_600_000
_DAY0 = date(2024, 1, 1)
_T0 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
_MARKET = Decimal("0.0001")
_N_HOURS = 24 * 183  # through 2024-07-01: test windows are April, May and June


def _candles() -> list[FundingCandle]:
    return [FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2",
                          mts=_T0 + i * _HOUR, close=_MARKET) for i in range(_N_HOURS)]


def _write_tape(root: Path, *, rate: float, days: int = 184) -> None:
    """Four borrower-initiated 5,000 trades a day at `rate`, 2-day period."""
    for d in range(days):
        day_ms = _T0 + d * 24 * _HOUR
        write_day(root, "fUST", _DAY0 + timedelta(days=d), [
            (d * 10 + k, day_ms + k * 6 * _HOUR + 1, -5000.0, rate, 2) for k in range(4)
        ])


def _market(offset: str = "0") -> ArmSpec:
    def make() -> QuoteOffset:
        inner = research_strategy("AlwaysMarketRateStrategy").create(period_days=2)
        return QuoteOffset(inner, Decimal(offset))
    return ArmSpec("always_2d" if offset == "0" else f"off_{offset}", "p2", make)


def _run(tmp_path: Path, arms: list[SearchArm]):
    series = {"p2": _candles()}
    windows = compute_wfo_windows(series["p2"])
    runs = evaluate_search_arms(series, windows, arms, TapeDays(tmp_path, "fUST"),
                                offer_amount=Decimal("1000"))
    return build_search_report(runs, baseline="always_2d", run=SearchRun(
        symbol="fUST", data_window="2024-01-01 .. 2024-07-01", n_trials=42))


def test_quote_offset_scales_the_rate_and_keeps_the_period() -> None:
    inner = research_strategy("AlwaysMarketRateStrategy").create(period_days=7)
    strategy = QuoteOffset(inner, Decimal("0.5"))
    candle = _candles()[0]
    decision = strategy.decide(candle)
    assert decision is not None
    assert decision.rate == Decimal("0.00015")
    assert decision.period_days == 7
    blank = candle.model_copy(update={"close": None})
    assert strategy.decide(blank) is None


def test_higher_quote_that_fills_in_both_bounds_is_positive(tmp_path: Path) -> None:
    _write_tape(tmp_path, rate=0.0002)  # above both quotes: price priority fills both
    report = _run(tmp_path, [SearchArm(_market()), SearchArm(_market("0.5"))])
    assert report.n_windows == 3
    assert report.verdicts == {"off_0.5": VERDICT_POSITIVE}
    for bound in ("conservative", "optimistic"):
        lo, hi = report.by_bound[bound].pair_mean_ci["off_0.5_vs_always_2d"]
        assert lo > 0 and hi > 0


def test_quote_at_the_traded_rate_waits_for_live(tmp_path: Path) -> None:
    # Every trade prints exactly at the arm's quote: the conservative bound counts
    # none of it (queue unknown), the optimistic bound all of it.
    _write_tape(tmp_path, rate=0.00015)
    report = _run(tmp_path, [SearchArm(_market()), SearchArm(_market("0.5"))])
    cons = report.by_bound["conservative"].arm_summaries
    opt = report.by_bound["optimistic"].arm_summaries
    assert cons["off_0.5"].mean_monthly == 0
    assert opt["off_0.5"].mean_monthly > opt["always_2d"].mean_monthly
    assert report.verdicts == {"off_0.5": VERDICT_WAIT_FOR_LIVE}


def test_shorter_wait_reprices_sooner(tmp_path: Path) -> None:
    # Nothing ever trades at or above the high quote, so that arm never fills;
    # the wait only changes how many offers it places.
    _write_tape(tmp_path, rate=0.00012)
    spec = _market("0.5")
    runs = evaluate_search_arms(
        {"p2": _candles()}, compute_wfo_windows(_candles()),
        [SearchArm(_market()), SearchArm(ArmSpec("w6", "p2", spec.make_strategy), max_wait_h=6),
         SearchArm(ArmSpec("w24", "p2", spec.make_strategy), max_wait_h=24)],
        TapeDays(tmp_path, "fUST"), offer_amount=Decimal("1000"))
    w6 = runs["conservative"]["w6"].outcomes
    w24 = runs["conservative"]["w24"].outcomes
    assert all(a.n_trades > b.n_trades for a, b in zip(w6, w24, strict=True))


def test_report_carries_trials_and_deflated_sharpe(tmp_path: Path) -> None:
    _write_tape(tmp_path, rate=0.0002)
    report = _run(tmp_path, [SearchArm(_market()), SearchArm(_market("0.5"))])
    payload = search_report_to_json([report])
    assert payload["n_trials"] == 42
    symbol = payload["symbols"][0]
    assert symbol["n_configs"] == 1  # the baseline is not a trial
    assert set(symbol["deflated_sharpe"]) == {"conservative", "optimistic"}
    assert set(symbol["deflated_sharpe"]["conservative"]) == {"always_2d", "off_0.5"}
    assert all(v is not None for v in symbol["deflated_sharpe"]["conservative"].values())
    assert symbol["verdicts"] == {"off_0.5": VERDICT_POSITIVE}
    markdown = render_search_markdown([report], caveats=["caveat one"])
    assert "n_trials=42" in markdown
    assert "| off_0.5 |" in markdown and "caveat one" in markdown


def test_arm_without_tape_is_reported_not_scored(tmp_path: Path) -> None:
    _write_tape(tmp_path, rate=0.0002, days=100)  # tape ends 2024-04-09, inside April
    report = _run(tmp_path, [SearchArm(_market()), SearchArm(_market("0.5"))])
    assert report.verdicts == {"off_0.5": VERDICT_NOT_SCORED}
    assert any("tape_day_missing" in note for note in report.by_bound["conservative"].notes)


def test_windows_are_limited_to_tape_coverage(tmp_path: Path) -> None:
    _write_tape(tmp_path, rate=0.0002, days=121)  # 2024-01-01 .. 2024-04-30
    first, last = tape_coverage(tmp_path, "fUST")
    assert (first, last) == (date(2024, 1, 1), date(2024, 4, 30))
    windows = compute_wfo_windows(_candles())
    kept = windows_within_tape(windows, first, last)
    assert [datetime.fromtimestamp(w.test_start_mts / 1000, UTC).month for w in kept] == [4]
    # A tape starting after the test month began cannot score that window either.
    assert windows_within_tape(windows, date(2024, 4, 2), date(2024, 6, 30)) == windows[1:]


def test_tape_coverage_fails_without_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no tape day files"):
        tape_coverage(tmp_path, "fUST")


def test_baseline_must_be_among_the_arms(tmp_path: Path) -> None:
    _write_tape(tmp_path, rate=0.0002)
    runs = evaluate_search_arms({"p2": _candles()}, compute_wfo_windows(_candles()),
                                [SearchArm(_market("0.5"))], TapeDays(tmp_path, "fUST"),
                                offer_amount=Decimal("1000"))
    with pytest.raises(ValueError, match="baseline"):
        build_search_report(runs, baseline="always_2d",
                            run=SearchRun(symbol="fUST", data_window="x", n_trials=42))
