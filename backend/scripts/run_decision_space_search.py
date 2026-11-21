"""Decision-space search on the trade-tape replay — research-gated, read-only.

Replays arms that differ in quote offset, lock tenor and reprice wait against
the public trade tape (daily files written by `scripts.ingest_public_trades`)
under the conservative and the optimistic fill bound, on the calendar WFO
windows, and scores each arm against `always_2d`.

Topics (pre-register the declared space in the strategy registry before running;
a finished run is never extended, a new space is a new registry row):

  period-arms   the deployed-parameter tenor arms (always_30d, mr_p2, mr_a30,
                adaptive_period) against always_2d. Their 4 trials are already in
                `DEFAULT_N_TRIALS`.
  grid          AlwaysMarketRate over --offsets x --periods x --wait-hours.
                Needs --n-trials: the registry's cumulative count including this
                grid's configs.

  fixtures (offline candles; the tape is still read from --tape-root):
    uv run python -m scripts.run_decision_space_search --topic period-arms \\
        --tape-root <tape> --fixtures fixtures/candles --output /tmp/search.md

  database (VM research container, candles from the DB, tape mounted read-only):
    python -m scripts.run_decision_space_search --topic period-arms \\
        --tape-root /tape --output /reports/<date>-decision-search-period-arms.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path

from bfx_funding_bot.apps.config import load_cells_only
from bfx_funding_bot.apps.research import research_strategy
from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.decision_search import (
    QuoteOffset,
    SearchArm,
    SearchReport,
    SearchRun,
    build_search_report,
    evaluate_search_arms,
    render_search_markdown,
    search_report_to_json,
    tape_coverage,
    windows_within_tape,
)
from bfx_funding_bot.modules.backtest.engine import resolve_market_series_key
from bfx_funding_bot.modules.backtest.fixture_io import load_candles
from bfx_funding_bot.modules.backtest.period_structure import (
    ARM_ALWAYS_2D,
    ArmSpec,
    align_series,
    clamp_to_joint_coverage,
    to_hourly_grid,
)
from bfx_funding_bot.modules.backtest.replay import TapeDays
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.strategy import CellConfig, Strategy
from scripts.run_oos_profitability import DEFAULT_N_TRIALS
from scripts.run_period_structure_backtest import (
    DEFAULT_AP_CELLS,
    DEFAULT_CELLS,
    DEFAULT_START,
    DEFAULT_SYMBOLS,
    SERIES_KEYS,
    ApParams,
    build_arms,
)

logger = logging.getLogger("run_decision_space_search")

TOPIC_PERIOD_ARMS = "period-arms"
TOPIC_GRID = "grid"
BASELINE_WAIT_H = 24

STANDING_CAVEATS = (
    "Replay not formally accepted: it was checked against 38 of our own fUST 2-day offers "
    "with post-hoc adjustments (92.1% bound coverage); other tenors and fUSD have no own-fill "
    "check. Verdicts here do not justify promotion.",
    "Same-rate trades: the conservative bound counts none, the optimistic bound all. A pair is "
    "called only when its mean-difference CI has one sign under both; otherwise wait for live.",
    "A trade can fill an offer of period P only when its public PERIOD <= P (exact if PERIOD is "
    "the bid's tenor, conservative if it is the offer's).",
    "FRR fills cannot be told apart on the tape and are not excluded (optimistic bias).",
    "No market-impact model: every offer is --offer-amount against the full recorded volume; "
    "fUST tenors of 15 days and more trade 150-472 a day at p10.",
    "Locks are truncated at window end, every window starts with free capital, and the last "
    "wait of each window places no offer.",
    "Deployed MR/AP params were selected on 2022-2026 data, in-sample to selection.",
    "Platform/credit tail (Bitfinex, Tether) is not backtestable; longer locks lengthen it.",
)


@dataclass(frozen=True)
class Grid:
    offsets: tuple[Decimal, ...]
    periods: tuple[int, ...]
    wait_hours: tuple[int, ...]

    def size(self) -> int:
        return len(self.grid_points())

    def grid_points(self) -> list[tuple[Decimal, int, int]]:
        baseline = (Decimal(0), 2, BASELINE_WAIT_H)
        points = [(o, p, w) for o in self.offsets for p in self.periods for w in self.wait_hours]
        return [point for point in dict.fromkeys(points) if point != baseline]


def _market_rate(period_days: int, offset: Decimal) -> Strategy:
    inner = research_strategy("AlwaysMarketRateStrategy").create(period_days=period_days)
    return QuoteOffset(inner, offset) if offset else inner


def _baseline_arm() -> SearchArm:
    return SearchArm(ArmSpec(ARM_ALWAYS_2D, "p2", lambda: _market_rate(2, Decimal(0))),
                     BASELINE_WAIT_H)


def grid_arms(grid: Grid, series: dict[str, list[FundingCandle]]) -> list[SearchArm]:
    """The baseline plus one arm per grid point, observing the tenor's own series."""
    arms = [_baseline_arm()]
    for offset, period, wait_h in grid.grid_points():
        key = resolve_market_series_key(period, series)
        arms.append(SearchArm(
            ArmSpec(f"mkt_p{period}_off{offset}_w{wait_h}", key,
                    partial(_market_rate, period, offset)),
            wait_h,
        ))
    return arms


def period_arms(
    *, symbol: str, series: dict[str, list[FundingCandle]], cells: Sequence[CellConfig],
    ap_cells: Sequence[CellConfig], ap: ApParams,
) -> tuple[list[SearchArm], list[str]]:
    """The period-structure arms that mean something on the tape.

    `mr_a30_legacy` only differs from `mr_a30` in which candle series prices the
    fill, and `always_frr` would be replayed as a fixed-rate offer at the FRR
    level, which is not how an FRR offer matches: both are left out.
    """
    specs, notes = build_arms(symbol=symbol, series=series, cells=cells, ap_cells=ap_cells,
                              ap=ap, frr_at=None)
    notes = [n for n in notes if not n.startswith("always_frr skipped")]
    notes.append("mr_a30_legacy and always_frr are not replayed: the tape prices every fill, "
                 "and an FRR offer does not match like a fixed-rate one")
    return [SearchArm(spec, BASELINE_WAIT_H) for spec in specs if spec.period_aware], notes


@dataclass(frozen=True)
class SearchSetup:
    """Everything a run shares across symbols."""

    tape_root: Path
    grid: Grid | None  # None = the period-arms topic
    n_trials: int
    offer_amount: Decimal
    cells: Sequence[CellConfig]
    ap_cells: Sequence[CellConfig]
    ap: ApParams
    p30_staleness_hours: int


def run_symbol(
    symbol: str, raw_series: dict[str, list[FundingCandle]], setup: SearchSetup
) -> SearchReport:
    series = dict(raw_series)
    if "p30" in series:
        series["p30"] = to_hourly_grid(series["p30"], max_gap_hours=setup.p30_staleness_hours)
    series = clamp_to_joint_coverage(series)
    series, dropped = align_series(series)
    if "p2" not in series:
        raise SystemExit(f"{symbol}: p2 series is required")
    first_day, last_day = tape_coverage(setup.tape_root, symbol)
    all_windows = compute_wfo_windows(series["p2"])
    windows = windows_within_tape(all_windows, first_day, last_day)
    if not windows:
        raise SystemExit(f"{symbol}: no WFO window inside the tape ({first_day} .. {last_day})")

    if setup.grid is not None:
        arms: list[SearchArm] = grid_arms(setup.grid, series)
        notes: list[str] = []
    else:
        arms, notes = period_arms(symbol=symbol, series=series, cells=setup.cells,
                                  ap_cells=setup.ap_cells, ap=setup.ap)
    notes.append(f"tape: {first_day} .. {last_day}; {len(windows)} of {len(all_windows)} WFO "
                 "windows have their test month inside it")
    notes.append(f"offer amount {setup.offer_amount}; series: "
                 + ", ".join(f"{k}={len(v)} candles" for k, v in sorted(series.items()))
                 + (f"; dropped {dropped} slots not present in every series" if dropped else ""))
    runs = evaluate_search_arms(series, windows, arms, TapeDays(setup.tape_root, symbol),
                                offer_amount=setup.offer_amount)
    p2 = series["p2"]
    first = datetime.fromtimestamp(p2[0].mts / 1000, UTC)
    last = datetime.fromtimestamp(p2[-1].mts / 1000, UTC)
    logger.info("%s: %d windows, %d arms", symbol, len(windows), len(arms))
    return build_search_report(runs, baseline=ARM_ALWAYS_2D, run=SearchRun(
        symbol=symbol, data_window=f"{first:%Y-%m-%d} .. {last:%Y-%m-%d}",
        n_trials=setup.n_trials, notes=tuple(notes),
    ))


def _load_fixture_series(directory: Path, symbol: str) -> dict[str, list[FundingCandle]]:
    paths = {key: directory / f"{symbol}_{key}_1h.jsonl.gz" for key in SERIES_KEYS}
    return {key: load_candles(path) for key, path in paths.items() if path.exists()}


async def _load_db_series(symbols: Sequence[str], start_mts: int) -> dict[str, dict[str, list[FundingCandle]]]:
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    engine = make_engine(Settings())
    session_factory = make_session_factory(engine)
    out: dict[str, dict[str, list[FundingCandle]]] = {}
    try:
        for symbol in symbols:
            async with session_scope(session_factory) as session:
                loaded = {
                    key: await get_candles_in_range(
                        session, symbol=symbol, timeframe="1h", period_agg=key,
                        start_mts=start_mts, end_mts=end_mts)
                    for key in SERIES_KEYS
                }
            out[symbol] = {key: candles for key, candles in loaded.items() if candles}
    finally:
        await engine.dispose()
    return out


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--topic", required=True, choices=[TOPIC_PERIOD_ARMS, TOPIC_GRID])
    p.add_argument("--tape-root", type=Path, required=True)
    p.add_argument("--output", required=True, help="markdown path (.json sibling auto)")
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--fixtures", type=Path, default=None, help="frozen candle dir (offline mode)")
    p.add_argument("--start", default=DEFAULT_START.strftime("%Y-%m-%d"), help="DB mode start (UTC)")
    p.add_argument("--offer-amount", default="1000")
    p.add_argument("--offsets", default=None, help="grid: quote offsets over market, e.g. 0,0.02,0.05")
    p.add_argument("--periods", default=None, help="grid: lock tenors in days, e.g. 2,30")
    p.add_argument("--wait-hours", default=None, help="grid: hours an unfilled offer rests, e.g. 6,24")
    p.add_argument("--n-trials", type=int, default=None,
                   help="cumulative registry trials including this run (required for grid)")
    p.add_argument("--cells", type=Path, default=DEFAULT_CELLS)
    p.add_argument("--ap-cells", type=Path, default=DEFAULT_AP_CELLS)
    p.add_argument("--ap-ema-span", type=int, default=24)
    p.add_argument("--ap-t1", default="0.5")
    p.add_argument("--ap-t2", default="2.0")
    p.add_argument("--ap-p-mid", type=int, default=7)
    p.add_argument("--ap-p-long", type=int, default=14)
    p.add_argument("--p30-staleness-hours", type=int, default=12)
    args = p.parse_args(list(argv))
    grid_flags = (args.offsets, args.periods, args.wait_hours)
    if args.topic == TOPIC_GRID:
        if None in grid_flags or args.n_trials is None:
            p.error("--topic grid needs --offsets, --periods, --wait-hours and --n-trials")
        args.grid = Grid(
            offsets=tuple(Decimal(v) for v in _csv(args.offsets)),
            periods=tuple(int(v) for v in _csv(args.periods)),
            wait_hours=tuple(int(v) for v in _csv(args.wait_hours)),
        )
        floor = DEFAULT_N_TRIALS + args.grid.size()
        if args.n_trials < floor:
            p.error(f"--n-trials {args.n_trials} undercounts: {DEFAULT_N_TRIALS} already "
                    f"registered + {args.grid.size()} grid configs = {floor}")
    else:
        if any(flag is not None for flag in grid_flags):
            p.error("--offsets/--periods/--wait-hours only apply to --topic grid")
        if args.n_trials is not None and args.n_trials < DEFAULT_N_TRIALS:
            p.error(f"--n-trials {args.n_trials} is below the registered {DEFAULT_N_TRIALS}")
        args.grid = None
        args.n_trials = args.n_trials if args.n_trials is not None else DEFAULT_N_TRIALS
    return args


async def _amain(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    symbols = _csv(args.symbols)
    setup = SearchSetup(
        tape_root=args.tape_root, grid=args.grid, n_trials=args.n_trials,
        offer_amount=Decimal(args.offer_amount), cells=load_cells_only(args.cells),
        ap_cells=load_cells_only(args.ap_cells) if args.ap_cells.exists() else [],
        ap=ApParams(ema_span=args.ap_ema_span, t1=Decimal(args.ap_t1), t2=Decimal(args.ap_t2),
                    p_mid=args.ap_p_mid, p_long=args.ap_p_long),
        p30_staleness_hours=args.p30_staleness_hours,
    )
    if args.fixtures is not None:
        raw_by_symbol = {s: _load_fixture_series(args.fixtures, s) for s in symbols}
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
        raw_by_symbol = await _load_db_series(symbols, int(start.timestamp() * 1000))

    reports: list[SearchReport] = []
    for symbol in symbols:
        raw = raw_by_symbol.get(symbol)
        if not raw:
            logger.warning("%s: no candles, skipping", symbol)
            continue
        reports.append(run_symbol(symbol, raw, setup))
    if not reports:
        logger.error("no symbol produced a report")
        return 2
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_search_markdown(reports, caveats=STANDING_CAVEATS))
    out.with_suffix(".json").write_text(json.dumps(search_report_to_json(reports), indent=2))
    logger.info("wrote %s and %s", out, out.with_suffix(".json"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain(sys.argv[1:])))
