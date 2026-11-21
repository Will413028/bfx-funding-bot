"""Decision-space search on the trade-tape replay (pure, no DB).

Arms differ in what they decide: quote offset over the observed market rate,
lock tenor, and how long an unfilled offer rests before the strategy decides
again (`max_wait_h`, the reprice rule). Each arm is replayed against the public
trade tape under both fill bounds (`backtest/replay.py`) on the calendar WFO
windows, and scored against a baseline arm by paired monthly differences.

A pair is called only when both bounds agree: the mean-difference CI is above 0
under both, or below 0 under both. Anything else is `wait_for_live`: the
answer depends on same-rate queue position, which the tape cannot settle.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, get_args

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import BacktestIncomplete, run_backtest
from bfx_funding_bot.modules.backtest.period_structure import (
    ArmRun,
    ArmSpec,
    PeriodStructureReport,
    build_report,
    symbol_to_json,
)
from bfx_funding_bot.modules.backtest.replay import TapeDays
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.performance.oos_profitability import (
    WindowOutcome,
    deflated_sharpe,
    sharpe_skew_kurt,
)
from bfx_funding_bot.modules.strategy import LendDecision, Strategy, StrategyDiagnostics

Bound = Literal["conservative", "optimistic"]
BOUNDS: tuple[Bound, ...] = get_args(Bound)

VERDICT_POSITIVE = "positive_both_bounds"
VERDICT_NEGATIVE = "negative_both_bounds"
VERDICT_WAIT_FOR_LIVE = "wait_for_live"
VERDICT_NOT_SCORED = "not_scored"

_MIN_WINDOWS_FOR_SHARPE = 3


class QuoteOffset:
    """Quote `inner`'s rate times (1 + offset); everything else is `inner`'s."""

    def __init__(self, inner: Strategy, offset: Decimal) -> None:
        if offset <= -1:
            raise ValueError(f"offset must be > -1, got {offset}")
        self._inner = inner
        self._offset = offset

    @property
    def name(self) -> str:
        return f"{self._inner.name}_off{self._offset}"

    def observe(self, candle: FundingCandle) -> None:
        self._inner.observe(candle)

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        decision = self._inner.decide(candle)
        if decision is None:
            return None
        return decision.model_copy(update={"rate": decision.rate * (1 + self._offset)})

    def diagnostics(self) -> StrategyDiagnostics:
        return self._inner.diagnostics()


@dataclass(frozen=True)
class SearchArm:
    spec: ArmSpec
    max_wait_h: int = 24


def tape_coverage(root: Path, symbol: str) -> tuple[date, date]:
    """First and last UTC day that has a tape file for `symbol`."""
    days = sorted(date.fromisoformat(path.name.removesuffix(".csv.gz"))
                  for path in (root / symbol).glob("*.csv.gz"))
    if not days:
        raise ValueError(f"no tape day files under {root / symbol}")
    return days[0], days[-1]


def _day_start_mts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def windows_within_tape(
    windows: Sequence[WfoWindow], first_day: date, last_day: date
) -> list[WfoWindow]:
    """Windows whose test month lies inside the tape; the train part only observes candles."""
    lo = _day_start_mts(first_day)
    hi = _day_start_mts(last_day + timedelta(days=1))
    return [w for w in windows if w.test_start_mts >= lo and w.test_end_mts < hi]


def _replay_arms(
    series: Mapping[str, list[FundingCandle]],
    windows: Sequence[WfoWindow],
    specs: Sequence[ArmSpec],
    config: BacktestConfig,
    tape: TapeDays,
) -> dict[str, ArmRun]:
    """Fresh strategy per window per arm; an arm the tape cannot cover is marked, not scored."""
    runs = {spec.name: ArmRun(outcomes=[]) for spec in specs}
    for w in windows:
        for spec in specs:
            if runs[spec.name].incomplete_reason is not None:
                continue
            observed = [c for c in series[spec.observe_key]
                        if w.train_start_mts <= c.mts <= w.test_end_mts]
            try:
                result = run_backtest(observed, spec.make_strategy(), config,
                                      w.test_start_mts, w.test_end_mts, tape=tape)
            except BacktestIncomplete as error:
                runs[spec.name] = ArmRun(outcomes=[], incomplete_reason=error.reason)
                continue
            runs[spec.name].outcomes.append(WindowOutcome(
                month_mts=w.test_start_mts, net_monthly=result.net_monthly_return_pct,
                n_trades=result.n_trades, fill_rate=result.fill_rate,
            ))
    return runs


def evaluate_search_arms(
    series: Mapping[str, list[FundingCandle]],
    windows: Sequence[WfoWindow],
    arms: Sequence[SearchArm],
    tape: TapeDays,
    *,
    offer_amount: Decimal,
) -> dict[Bound, dict[str, ArmRun]]:
    """Replay every arm on every window under each bound; bound -> arm name -> run."""
    names = [arm.spec.name for arm in arms]
    if len(set(names)) != len(names):
        raise ValueError(f"arm names must be unique, got {names}")
    waits = sorted({arm.max_wait_h for arm in arms})
    runs: dict[Bound, dict[str, ArmRun]] = {}
    for bound in BOUNDS:
        by_arm: dict[str, ArmRun] = {}
        for wait_h in waits:
            config = BacktestConfig(
                fill_model="tape-replay", replay_bound=bound, replay_max_wait_h=wait_h,
                replay_offer_amount=offer_amount, truncate_at_window_end=True,
            )
            specs = [arm.spec for arm in arms if arm.max_wait_h == wait_h]
            by_arm.update(_replay_arms(series, windows, specs, config, tape))
        runs[bound] = {name: by_arm[name] for name in names}
    return runs


@dataclass(frozen=True)
class SearchReport:
    symbol: str
    baseline: str
    n_windows: int
    n_trials: int
    n_configs: int  # arms scored against the baseline in this run
    by_bound: dict[Bound, PeriodStructureReport]
    deflated_sharpe: dict[Bound, dict[str, Decimal | None]]
    verdicts: dict[str, str]


def _pair_label(arm: str, baseline: str) -> str:
    return f"{arm}_vs_{baseline}"


def _verdict(cis: Sequence[tuple[Decimal, Decimal] | None]) -> str:
    known = [ci for ci in cis if ci is not None]
    if len(known) != len(cis):
        return VERDICT_NOT_SCORED
    if all(lo > 0 for lo, _hi in known):
        return VERDICT_POSITIVE
    if all(hi < 0 for _lo, hi in known):
        return VERDICT_NEGATIVE
    return VERDICT_WAIT_FOR_LIVE


def _deflated_sharpes(
    report: PeriodStructureReport, n_trials: int
) -> dict[str, Decimal | None]:
    out: dict[str, Decimal | None] = {}
    for arm, windows in report.windows.items():
        monthly = [value / Decimal(100) for _mts, value in windows]
        if len(monthly) < _MIN_WINDOWS_FOR_SHARPE:
            out[arm] = None
            continue
        sharpe, skew, kurtosis = sharpe_skew_kurt(monthly)
        out[arm] = deflated_sharpe(sharpe, n_trials=n_trials, n_obs=len(monthly),
                                   skew=skew, kurtosis=kurtosis)
    return out


@dataclass(frozen=True)
class SearchRun:
    """What one symbol's search ran on, as the report states it."""

    symbol: str
    data_window: str
    n_trials: int  # cumulative registered trials, the deflated-Sharpe input
    notes: tuple[str, ...] = ()


def build_search_report(
    runs_by_bound: Mapping[Bound, Mapping[str, ArmRun]], *, baseline: str, run: SearchRun
) -> SearchReport:
    arm_names = list(runs_by_bound[BOUNDS[0]])
    if baseline not in arm_names:
        raise ValueError(f"baseline arm {baseline!r} is not among the arms {arm_names}")
    challengers = [name for name in arm_names if name != baseline]
    pairs = [(_pair_label(name, baseline), name, baseline) for name in challengers]
    by_bound = {
        bound: build_report(symbol=run.symbol, runs=runs_by_bound[bound],
                            data_window=run.data_window, pairs=pairs, notes=run.notes)
        for bound in BOUNDS
    }
    verdicts = {
        name: _verdict([by_bound[bound].pair_mean_ci.get(_pair_label(name, baseline))
                        for bound in BOUNDS])
        for name in challengers
    }
    return SearchReport(
        symbol=run.symbol, baseline=baseline,
        n_windows=min(report.n_windows for report in by_bound.values()),
        n_trials=run.n_trials, n_configs=len(challengers), by_bound=by_bound,
        deflated_sharpe={bound: _deflated_sharpes(by_bound[bound], run.n_trials)
                         for bound in BOUNDS},
        verdicts=verdicts,
    )


def _f(x: Decimal | None, nd: int = 4) -> str:
    if x is None:
        return "-"
    return "inf" if not x.is_finite() else f"{x:.{nd}f}"


def render_search_markdown(reports: Sequence[SearchReport], *, caveats: Sequence[str]) -> str:
    lines = [
        "# Decision-space search on the trade-tape replay",
        "",
        f"**Run date**: {datetime.now(UTC).isoformat()}",
        "**Fill model**: tape-replay, conservative and optimistic bounds "
        "(same-rate trades: none / all).",
        "**Windows**: calendar WFO (train 3mo warmup / test 1mo / step 1mo), fresh state per "
        "window, locks truncated at window end.",
        "",
        "## Standing caveats",
        "",
        *(f"- {c}" for c in caveats),
    ]
    for r in reports:
        first = r.by_bound[BOUNDS[0]]
        lines += [
            "",
            f"## {r.symbol} — {r.n_windows} monthly OOS windows ({first.data_window})",
            "",
            f"Trials: n_trials={r.n_trials} (cumulative, deflated Sharpe input); "
            f"{r.n_configs} configs scored against `{r.baseline}` in this run.",
            "",
            *(f"- {n}" for n in first.notes),
            "",
            f"### Paired monthly differences vs {r.baseline} (% per month)",
            "",
            "| arm | conservative mean [95% CI] | optimistic mean [95% CI] | verdict |",
            "|---|---|---|---|",
        ]
        for arm, verdict in r.verdicts.items():
            cells = []
            for bound in BOUNDS:
                report = r.by_bound[bound]
                label = _pair_label(arm, r.baseline)
                if label in report.pair_summaries:
                    lo, hi = report.pair_mean_ci[label]
                    cells.append(f"{_f(report.pair_summaries[label].mean_active)} "
                                 f"[{_f(lo)}, {_f(hi)}]")
                else:
                    cells.append("-")
            lines.append(f"| {arm} | {cells[0]} | {cells[1]} | {verdict} |")
        for bound in BOUNDS:
            report = r.by_bound[bound]
            lines += [
                "",
                f"### Arms, {bound} bound",
                "",
                "| arm | annualized net % | median monthly % | p25 | worst | mean | idle rate "
                "| mean fill | deflated Sharpe |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for name, s in report.arm_summaries.items():
                lines.append(
                    f"| {name} | {_f(s.annualized_pct, 2)} | {_f(s.median_monthly)} | "
                    f"{_f(s.p25_monthly)} | {_f(s.worst_monthly)} | {_f(s.mean_monthly)} | "
                    f"{_f(s.idle_rate, 2)} | {_f(s.mean_fill_rate)} | "
                    f"{_f(r.deflated_sharpe[bound].get(name))} |"
                )
            unscored = [n for n in report.notes if n not in first.notes]
            lines += [f"- {n}" for n in unscored]
    return "\n".join(lines) + "\n"


def search_report_to_json(reports: Sequence[SearchReport]) -> dict[str, object]:
    if len({r.n_trials for r in reports}) > 1:
        raise ValueError("reports in one file must share n_trials")
    return {
        "fill_model": "tape-replay",
        "n_trials": reports[0].n_trials if reports else None,
        "symbols": [
            {
                "symbol": r.symbol, "baseline": r.baseline, "n_windows": r.n_windows,
                "n_configs": r.n_configs, "verdicts": r.verdicts,
                "deflated_sharpe": {
                    bound: {arm: (str(v) if v is not None else None) for arm, v in values.items()}
                    for bound, values in r.deflated_sharpe.items()
                },
                "bounds": {bound: symbol_to_json(r.by_bound[bound]) for bound in BOUNDS},
            }
            for r in reports
        ],
    }
