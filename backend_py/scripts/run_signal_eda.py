"""Signal EDA funnel driver.

Pulls candles + funding_stats from the configured DB (read-only SELECT), aligns
them, scores 4 candidate signals across cell x horizon x regime, applies BH-FDR,
and writes a GO/KILL markdown + json report. See
docs/superpowers/specs/2026-06-05-signal-eda-funnel-design.md.

Run:  cd backend_py && uv run python -m scripts.run_signal_eda \
      --output docs/research/2026-06-06-signal-eda-funnel.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
    render_report,
    split_regime,
)
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.repository import get_in_range as get_stats_in_range
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.marketfeed.config import CellConfig, load_cells_only

logger = logging.getLogger(__name__)
START_MTS = 1_451_606_400_000  # 2016-01-01 UTC (full history)
DEFAULT_CELLS_YAML = Path("configs/cells.canary.yaml")
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


def run_funnel_for_cell(
    cell_label: str, candles: list[FundingCandle], stats: list[FundingStat]
) -> list[_Obs]:
    """Build the aligned frame, then compute one IC observation per
    signal x horizon x regime. Pure (no I/O) so it is unit-testable."""
    if not candles or not stats:
        return []
    frame = add_forward_rate_change(build_signal_frame(candles, stats), HORIZONS)
    early, late = split_regime(frame)
    out: list[_Obs] = []
    for regime_name, sub in (("early", early), ("late", late)):
        if len(sub) < _MIN_REGIME_ROWS:
            continue
        for sig_name, fn in SIGNALS.items():
            sig = fn(sub)
            for h in HORIZONS:
                res = block_bootstrap_ic(sig, sub[f"fwd_d{h}"])
                out.append(_Obs(sig_name, cell_label, regime_name, h, res.point, res.p_value))
    return out


def _apply_fdr_and_decide(all_obs: list[_Obs]) -> list[SignalVerdict]:
    """BH-FDR across ALL observations, then reduce to per-signal verdicts."""
    pvals = [o.p_value for o in all_obs]
    rejected = bh_fdr(pvals)
    by_signal: dict[str, list[CellRegimeIC]] = {}
    for o, sig in zip(all_obs, rejected, strict=True):
        by_signal.setdefault(o.signal_name, []).append(
            CellRegimeIC(cell=o.cell, regime=o.regime, horizon=o.horizon,
                         ic=o.ic, fdr_significant=sig)
        )
    return [decide_signal(name, obs) for name, obs in sorted(by_signal.items())]


async def _run_cell(session: AsyncSession, cell: CellConfig, end_mts: int) -> list[_Obs]:
    candles = await get_candles_in_range(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, start_mts=START_MTS, end_mts=end_mts,
    )
    stats = await get_stats_in_range(session, symbol=cell.symbol, start_mts=START_MTS, end_mts=end_mts)
    return run_funnel_for_cell(cell.cell_id, candles, stats)


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
    all_obs: list[_Obs] = []
    try:
        for cell in load_cells_only(Path(args.cells)):
            async with session_scope(session_factory) as session:
                cell_obs = await _run_cell(session, cell, end_mts)
                logger.info("%s: %d observations", cell.cell_id, len(cell_obs))
                all_obs.extend(cell_obs)
    finally:
        await engine.dispose()

    verdicts = _apply_fdr_and_decide(all_obs)
    n_cells = len({o.cell for o in all_obs})
    md = render_report(
        verdicts, data_window=f"{START_MTS}-now, {n_cells} cells, {len(all_obs)} observations"
    )
    out_path = Path(args.output)
    out_path.write_text(md)
    out_path.with_suffix(".json").write_text(
        json.dumps(
            [
                {"signal": v.signal, "verdict": v.verdict, "median_ic": v.median_ic, "reason": v.reason}
                for v in verdicts
            ],
            indent=2,
        )
    )
    logger.info("wrote %s (+ .json)", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
