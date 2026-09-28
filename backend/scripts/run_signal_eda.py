"""Signal EDA funnel driver.

Pulls candles + funding_stats from the configured DB (read-only SELECT), aligns
them, scores 4 candidate signals across cell x horizon x regime, applies BH-FDR,
and writes a GO/KILL markdown + json report.

Run:  cd backend && uv run python -m scripts.run_signal_eda \
      --output /tmp/2026-06-06-signal-eda-funnel.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.signal_eda import (
    HORIZONS,
    SIGNALS,
    CellRegimeIC,
    SignalVerdict,
    add_forward_rate_change,
    bh_fdr,
    block_bootstrap_ic,
    build_signal_frame,
    decide_signal,
    quintile_spread,
    render_report,
    resample_daily,
    split_regime,
)
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.repository import get_in_range as get_stats_in_range
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.marketfeed.config import load_cells_only
from bfx_funding_bot.modules.strategy import CellConfig

logger = logging.getLogger(__name__)
START_MTS = 1_451_606_400_000  # 2016-01-01 UTC (full history)
DEFAULT_CELLS_YAML = Path("configs/cells.live.yaml")
_MIN_REGIME_ROWS = 30


@dataclass(frozen=True)
class _Obs:
    """One IC observation before FDR (carries signal name + p-value for BH)."""

    signal_name: str
    cell: str
    regime: str
    horizon: int
    ic: float
    p_value: float
    quintile: float
    quintile_monotonic: bool


def run_funnel_for_cell(
    cell_label: str, candles: list[FundingCandle], stats: list[FundingStat]
) -> list[_Obs]:
    """Build the aligned frame, then compute one IC observation per
    signal x horizon x regime. Pure (no I/O) so it is unit-testable."""
    if not candles or not stats:
        return []
    # Resample hourly candles to daily so the signal rolling windows (w=14, w=30
    # rows) are day-scaled as intended, not ~24x too short.
    frame = resample_daily(build_signal_frame(candles, stats))
    frame = add_forward_rate_change(frame, HORIZONS)
    early, late = split_regime(frame)
    out: list[_Obs] = []
    for regime_name, sub in (("early", early), ("late", late)):
        if len(sub) < _MIN_REGIME_ROWS:
            continue
        for sig_name, fn in SIGNALS.items():
            sig = fn(sub)
            for h in HORIZONS:
                res = block_bootstrap_ic(sig, sub[f"fwd_d{h}"])
                qspread, qmono = quintile_spread(sig, sub[f"fwd_d{h}"])
                out.append(_Obs(sig_name, cell_label, regime_name, h, res.point, res.p_value,
                                qspread, qmono))
    return out


def _apply_fdr_and_decide(
    all_obs: list[_Obs],
) -> tuple[list[SignalVerdict], list[bool]]:
    """BH-FDR across ALL observations, then reduce to per-signal verdicts.

    Returns (verdicts, rejected_mask) where rejected_mask is aligned to all_obs
    order so the caller can annotate the audit grid with fdr_significant."""
    pvals = [o.p_value for o in all_obs]
    rejected = bh_fdr(pvals)
    by_signal: dict[str, list[CellRegimeIC]] = {}
    spreads_by_signal: dict[str, list[float]] = {}
    for o, sig in zip(all_obs, rejected, strict=True):
        by_signal.setdefault(o.signal_name, []).append(
            CellRegimeIC(cell=o.cell, regime=o.regime, horizon=o.horizon,
                         ic=o.ic, fdr_significant=sig)
        )
        # Gate 5 input: only FDR-significant observations' quintile spreads —
        # aligned with the population the statistical gates ran on.
        if sig and not np.isnan(o.quintile):
            spreads_by_signal.setdefault(o.signal_name, []).append(o.quintile)
    verdicts = [
        decide_signal(
            name, obs,
            median_quintile_spread=(
                float(np.median(spreads_by_signal[name]))
                if spreads_by_signal.get(name) else None
            ),
        )
        for name, obs in sorted(by_signal.items())
    ]
    return verdicts, rejected


async def _fetch_cell(
    session: AsyncSession, cell: CellConfig, end_mts: int
) -> tuple[str, list[FundingCandle], list[FundingStat]]:
    """Fetch raw candles + stats for one cell. DB I/O ONLY — the heavy pure-CPU
    funnel runs OUTSIDE the session scope. A long compute inside the session lets
    Neon (serverless) close the idle connection mid-operation."""
    candles = await get_candles_in_range(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, start_mts=START_MTS, end_mts=end_mts,
    )
    stats = await get_stats_in_range(session, symbol=cell.symbol, start_mts=START_MTS, end_mts=end_mts)
    return cell.cell_id, candles, stats


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="markdown output path (.json sibling auto)")
    parser.add_argument("--cells", default=str(DEFAULT_CELLS_YAML))
    args = parser.parse_args()

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    fetched: list[tuple[str, list[FundingCandle], list[FundingStat]]] = []
    try:
        for cell in load_cells_only(Path(args.cells)):
            async with session_scope(session_factory) as session:
                cell_id, candles, stats = await _fetch_cell(session, cell, end_mts)
                logger.info("%s: fetched %d candles / %d stats", cell_id, len(candles), len(stats))
                fetched.append((cell_id, candles, stats))
    finally:
        await engine.dispose()

    # Heavy pure-CPU funnel runs AFTER all DB sessions are closed (Neon would
    # otherwise drop the idle connection during the multi-minute compute).
    all_obs: list[_Obs] = []
    min_mts: int | None = None
    for cell_id, candles, stats in fetched:
        if candles:
            earliest = candles[0].mts  # candles are ASC by mts
            min_mts = earliest if min_mts is None else min(min_mts, earliest)
        cell_obs = run_funnel_for_cell(cell_id, candles, stats)
        logger.info("%s: %d observations", cell_id, len(cell_obs))
        all_obs.extend(cell_obs)

    verdicts, rejected_mask = _apply_fdr_and_decide(all_obs)
    n_cells = len({o.cell for o in all_obs})
    if min_mts is not None:
        start_date = datetime.fromtimestamp(min_mts / 1000, UTC).date()
    else:
        start_date = datetime.fromtimestamp(START_MTS / 1000, UTC).date()
    data_window = f"{start_date}-now, {n_cells} cells, {len(all_obs)} observations"
    md = render_report(verdicts, data_window=data_window)
    out_path = Path(args.output)
    out_path.write_text(md + "\nFull per-cell × regime × horizon IC / p-value / quintile grid in the .json sidecar.\n")
    out_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "verdicts": [
                    {"signal": v.signal, "verdict": v.verdict,
                     "median_ic": v.median_ic, "reason": v.reason}
                    for v in verdicts
                ],
                "observations": [
                    {
                        "signal": o.signal_name,
                        "cell": o.cell,
                        "regime": o.regime,
                        "horizon": o.horizon,
                        "ic": o.ic,
                        "p_value": o.p_value,
                        "fdr_significant": bool(sig),
                        "quintile_spread": o.quintile,
                        "quintile_monotonic": o.quintile_monotonic,
                    }
                    for o, sig in zip(all_obs, rejected_mask, strict=True)
                ],
            },
            indent=2,
        )
    )
    logger.info("wrote %s (+ .json)", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
