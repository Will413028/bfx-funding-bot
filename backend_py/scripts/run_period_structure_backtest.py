"""Period-structure four-arm backtest (Proposal D, 2026-09-22 review §6) — research-gated.

Scores lock-tenor arms against each other with fills priced off the market series
of the tenor actually posted (p2 / p30 / a30), so the a30 tenor mismatch that
inflated deployed a30 cells is gone and `always_30d` can be compared honestly
with `always_2d`. Pre-registered in the strategy registry on 2026-09-22
(4 new trials -> DEFAULT_N_TRIALS 38); do not extend the arm set without a new
registry row.

Two data modes:

  fixtures (offline, no DB — fUSD has p2/p30/a30, fUST has p2/a30; no FRR arm):
    cd backend_py
    uv run python -m scripts.run_period_structure_backtest --fixtures fixtures/candles \\
        --output docs/research/2026-09-22-period-structure-fixtures.md

  database (VM research container; adds fUST p30 and the always_frr arm):
    uv run python -m scripts.run_period_structure_backtest \\
        --output /reports/<date>-period-structure.md

Run twice: default `--fill-alpha 5.0` and `--fill-alpha 1e-9` (always-fill bound).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.fixture_io import load_candles
from bfx_funding_bot.modules.backtest.frr_series import FrrSeries
from bfx_funding_bot.modules.backtest.period_structure import (
    ARM_ADAPTIVE,
    ARM_ALWAYS_2D,
    ARM_ALWAYS_30D,
    ARM_ALWAYS_FRR,
    ARM_MR_A30,
    ARM_MR_A30_LEGACY,
    ARM_MR_P2,
    ArmSpec,
    PeriodStructureReport,
    build_report,
    clamp_to_joint_coverage,
    evaluate_period_arms,
    render_markdown,
    report_to_json,
    to_hourly_grid,
)
from bfx_funding_bot.modules.backtest.strategies.adaptive_period import (
    AdaptivePeriodStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFrrStrategy
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import (
    AlwaysMarketRateStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import MeanReversionStrategy
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.repository import get_in_range
from bfx_funding_bot.modules.live_validation.live_attribution import (
    FRR_ANNUALIZATION,
    assert_market_rate_band,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig, load_cells_only

logger = logging.getLogger("run_period_structure_backtest")

SERIES_KEYS = ("p2", "p30", "a30")
DEFAULT_SYMBOLS = "fUST,fUSD"
DEFAULT_CELLS = Path("configs/cells.yaml")
DEFAULT_AP_CELLS = Path("configs/cells.experimental-p14.yaml")
DEFAULT_START = datetime(2022, 1, 1, tzinfo=UTC)

STANDING_CAVEATS = (
    "Linear fill model: above-market quotes decay linearly (alpha=5 -> +10% = 50% fill); the "
    "book-replay model (Proposal C) replaces this. Run with --fill-alpha 1e-9 for the "
    "always-fill bound; a pair whose CI straddles 0 under both bounds is 'wait for live'.",
    "Tenor pricing: 2d -> p2, 30d -> p30, every other tenor (AP's 7/14) -> a30 aggregate, an "
    "approximation flagged per arm in the 'priced by' column.",
    "p30 is LOCF'd onto the hourly grid with the cells.yaml 12h staleness budget; slots beyond "
    "budget carry no decision.",
    "Locks are truncated at window end and every window starts with free capital; monthly "
    "figures are 'interest earned in-window on locks opened in-window'.",
    "Deployed MR/AP params were selected on 2022-2026 data, in-sample to selection.",
    "Platform/credit tail (Bitfinex, Tether) is not backtestable; longer locks lengthen it.",
)


@dataclass(frozen=True)
class ApParams:
    ema_span: int
    t1: Decimal
    t2: Decimal
    p_mid: int
    p_long: int


def _find_params(
    cells: Sequence[CellConfig], *, strategy: str, symbol: str, period_agg: str
) -> dict[str, object] | None:
    for cell in cells:
        if (
            cell.strategy.value == strategy
            and cell.symbol == symbol
            and cell.period_agg == period_agg
        ):
            return dict(cell.params)
    return None


def _mr_factory(params: dict[str, object]) -> Callable[[], Strategy]:
    ema_span = int(str(params["ema_span"]))
    threshold_sigma = Decimal(str(params["threshold_sigma"]))
    ratio_sigma = Decimal(str(params["ratio_sigma"]))
    return lambda: MeanReversionStrategy(
        ema_span=ema_span, threshold_sigma=threshold_sigma, ratio_sigma=ratio_sigma
    )


def build_arms(
    *,
    symbol: str,
    series: dict[str, list[FundingCandle]],
    cells: Sequence[CellConfig],
    ap_cells: Sequence[CellConfig],
    ap: ApParams,
    frr_at: Callable[[int], Decimal | None] | None,
) -> tuple[list[ArmSpec], list[str]]:
    """Arms available for this symbol given the series and params on hand, plus notes."""
    arms: list[ArmSpec] = [
        ArmSpec(ARM_ALWAYS_2D, "p2", lambda: AlwaysMarketRateStrategy(period_days=2)),
    ]
    notes: list[str] = []
    if "p30" in series:
        arms.append(
            ArmSpec(ARM_ALWAYS_30D, "p30", lambda: AlwaysMarketRateStrategy(period_days=30))
        )
    else:
        notes.append("always_30d skipped: no p30 series for this symbol in this data mode")

    mr_p2 = _find_params(cells, strategy="mean_reversion", symbol=symbol, period_agg="p2")
    if mr_p2 is not None:
        arms.append(ArmSpec(ARM_MR_P2, "p2", _mr_factory(mr_p2)))
    else:
        notes.append("mr_p2 skipped: no mean_reversion p2 cell in --cells")

    mr_a30 = _find_params(cells, strategy="mean_reversion", symbol=symbol, period_agg="a30")
    if mr_a30 is not None and "a30" in series:
        arms.append(ArmSpec(ARM_MR_A30, "a30", _mr_factory(mr_a30)))
        arms.append(
            ArmSpec(ARM_MR_A30_LEGACY, "a30", _mr_factory(mr_a30), period_aware=False)
        )
    else:
        notes.append("mr_a30 / mr_a30_legacy skipped: no mean_reversion a30 cell or a30 series")

    ap_cell = _find_params(ap_cells, strategy="adaptive_period", symbol=symbol, period_agg="a30")
    sigma_source = "ap cells" if ap_cell is not None else "mr a30 cell"
    sigma_raw = (ap_cell or mr_a30 or {}).get("ratio_sigma")
    if sigma_raw is not None and "a30" in series:
        sigma = Decimal(str(sigma_raw))
        arms.append(
            ArmSpec(
                ARM_ADAPTIVE, "a30",
                lambda: AdaptivePeriodStrategy(
                    ema_span=ap.ema_span, ratio_sigma=sigma, t1=ap.t1, t2=ap.t2,
                    p_mid=ap.p_mid, p_long=ap.p_long,
                ),
            )
        )
        notes.append(
            f"adaptive_period: ema_span={ap.ema_span} band=({ap.t1},{ap.t2}) "
            f"p_mid={ap.p_mid} p_long={ap.p_long} ratio_sigma={sigma} "
            f"(ADR 2026-06-04 D2/D3 locked band; ratio_sigma from {sigma_source})"
        )
    else:
        notes.append("adaptive_period skipped: no a30 ratio_sigma or a30 series")

    if frr_at is not None:
        arms.append(
            ArmSpec(ARM_ALWAYS_FRR, "p2", lambda: AlwaysFrrStrategy(frr_at=frr_at, period_days=2))
        )
    else:
        notes.append("always_frr skipped: funding_stats not available in this data mode")
    return arms, notes


def run_symbol(
    *,
    symbol: str,
    raw_series: dict[str, list[FundingCandle]],
    frr_series: FrrSeries | None,
    cells: Sequence[CellConfig],
    ap_cells: Sequence[CellConfig],
    ap: ApParams,
    fill_alpha: Decimal,
    p30_staleness_hours: int,
) -> PeriodStructureReport:
    series = dict(raw_series)
    if "p30" in series:
        series["p30"] = to_hourly_grid(series["p30"], max_gap_hours=p30_staleness_hours)
    series = clamp_to_joint_coverage(series)
    if "p2" not in series:
        raise SystemExit(f"{symbol}: p2 series is required")
    windows = compute_wfo_windows(series["p2"])
    if not windows:
        raise SystemExit(f"{symbol}: no WFO windows")

    arms, notes = build_arms(
        symbol=symbol, series=series, cells=cells, ap_cells=ap_cells, ap=ap,
        frr_at=frr_series.at if frr_series is not None else None,
    )
    config = BacktestConfig(
        fill_model="linear-baseline", fill_alpha=fill_alpha, truncate_at_window_end=True
    )
    runs = evaluate_period_arms(series, windows, arms, config)
    p2 = series["p2"]
    first = datetime.fromtimestamp(p2[0].mts / 1000, UTC)
    last = datetime.fromtimestamp(p2[-1].mts / 1000, UTC)
    notes.append("series: " + ", ".join(f"{k}={len(v)} candles" for k, v in sorted(series.items())))
    logger.info("%s: %d windows, %d arms", symbol, len(windows), len(arms))
    return build_report(
        symbol=symbol, runs=runs, data_window=f"{first:%Y-%m-%d} .. {last:%Y-%m-%d}", notes=notes,
    )


def _load_fixture_series(directory: Path, symbol: str) -> dict[str, list[FundingCandle]]:
    series: dict[str, list[FundingCandle]] = {}
    for key in SERIES_KEYS:
        path = directory / f"{symbol}_{key}_1h.jsonl.gz"
        if path.exists():
            series[key] = load_candles(path)
    return series


async def _load_db_series(
    session: AsyncSession, *, symbol: str, start_mts: int, end_mts: int
) -> tuple[dict[str, list[FundingCandle]], FrrSeries | None]:
    series: dict[str, list[FundingCandle]] = {}
    for key in SERIES_KEYS:
        candles = await get_candles_in_range(
            session, symbol=symbol, timeframe="1h", period_agg=key,
            start_mts=start_mts, end_mts=end_mts,
        )
        if candles:
            series[key] = candles
    stats = await get_in_range(session, symbol=symbol, start_mts=start_mts, end_mts=end_mts)
    frr = FrrSeries.from_stats(stats)
    if len(frr) == 0:
        return series, None
    assert_market_rate_band([s.frr * FRR_ANNUALIZATION for s in stats if s.frr is not None])
    frr_start = min(s.mts for s in stats if s.frr is not None)
    series = {k: [c for c in v if c.mts >= frr_start] for k, v in series.items()}
    return series, frr


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--output", required=True, help="markdown path (.json sibling auto)")
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--fixtures", type=Path, default=None, help="frozen candle dir (offline mode)")
    p.add_argument("--cells", type=Path, default=DEFAULT_CELLS)
    p.add_argument("--ap-cells", type=Path, default=DEFAULT_AP_CELLS)
    p.add_argument("--ap-ema-span", type=int, default=24)
    p.add_argument("--ap-t1", default="0.5")
    p.add_argument("--ap-t2", default="2.0")
    p.add_argument("--ap-p-mid", type=int, default=7)
    p.add_argument("--ap-p-long", type=int, default=14)
    p.add_argument("--fill-alpha", default="5.0")
    p.add_argument("--p30-staleness-hours", type=int, default=12)
    p.add_argument("--start", default=DEFAULT_START.strftime("%Y-%m-%d"), help="DB mode start (UTC)")
    return p.parse_args(list(argv))


async def _amain(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cells = load_cells_only(args.cells)
    ap_cells = load_cells_only(args.ap_cells) if args.ap_cells.exists() else []
    ap = ApParams(
        ema_span=args.ap_ema_span, t1=Decimal(args.ap_t1), t2=Decimal(args.ap_t2),
        p_mid=args.ap_p_mid, p_long=args.ap_p_long,
    )
    fill_alpha = Decimal(args.fill_alpha)

    def _run(
        symbol: str, raw: dict[str, list[FundingCandle]], frr: FrrSeries | None
    ) -> PeriodStructureReport:
        return run_symbol(
            symbol=symbol, raw_series=raw, frr_series=frr, cells=cells, ap_cells=ap_cells,
            ap=ap, fill_alpha=fill_alpha, p30_staleness_hours=args.p30_staleness_hours,
        )

    reports: list[PeriodStructureReport] = []
    if args.fixtures is not None:
        for symbol in symbols:
            raw = _load_fixture_series(args.fixtures, symbol)
            if not raw:
                logger.warning("%s: no fixture series under %s, skipping", symbol, args.fixtures)
                continue
            reports.append(_run(symbol, raw, None))
    else:
        start_mts = int(datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
        end_mts = int(datetime.now(UTC).timestamp() * 1000)
        engine = make_engine(Settings())
        session_factory = make_session_factory(engine)
        try:
            for symbol in symbols:
                async with session_scope(session_factory) as session:
                    raw, frr = await _load_db_series(
                        session, symbol=symbol, start_mts=start_mts, end_mts=end_mts
                    )
                if not raw:
                    logger.warning("%s: no candles in DB, skipping", symbol)
                    continue
                reports.append(_run(symbol, raw, frr))
        finally:
            await engine.dispose()

    if not reports:
        logger.error("no symbol produced a report")
        return 2
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(reports, fill_alpha=fill_alpha, caveats=STANDING_CAVEATS))
    out.with_suffix(".json").write_text(
        json.dumps(report_to_json(reports, fill_alpha=fill_alpha), indent=2)
    )
    logger.info("wrote %s and %s", out, out.with_suffix(".json"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain(sys.argv[1:])))
