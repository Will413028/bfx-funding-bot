"""Why does live state differ from replay? Reproduce both arms offline, side by side.

Three fixes shrank the live-vs-replay EMA gap (4.1e-2 -> 9.4e-3 -> 6.8e-3 -> 5.2e-3)
without ever closing it, and the sign never changed. Rather than guess a fourth
cause, this rebuilds both arms from one database snapshot and prints what each
actually observed.

The question it answers is a fork:
  - gap present immediately after warmup  -> the two WINDOWS differ (warmup reads a
    time range ending at boot; replay reads a row count ending at the boundary)
  - gap zero at warmup, growing per tick  -> the two OBSERVE SEQUENCES differ

Read-only. Touches no live state.

Usage (VM):
    cd backend
    uv run python scripts/diagnose_warmup_replay_parity.py --ticks 6
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.apps.config import load_cells_only
from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.candles.repository import get_candles_in_range, get_up_to
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.divergence_reporter import ExtractedSignal
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    build_strategy_at_boundary,
)
from bfx_funding_bot.modules.strategy import CellConfig

logger = logging.getLogger("parity")

_TIMEFRAME_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000}
DEFAULT_CELLS_YAML = Path("configs/cells.live.yaml")
_MR_LOOKBACK = 200  # warmup._lookback_for for MEAN_REVERSION


def _rel(a: Decimal | None, b: Decimal | None) -> str:
    if a is None or b is None:
        return "n/a"
    scale = max(abs(a), abs(b))
    if scale == 0:
        return "0"
    return f"{float(abs(a - b) / scale):.3e}"


def _span(candles: list[FundingCandle]) -> str:
    if not candles:
        return "empty"
    return f"{len(candles)} [{candles[0].mts} .. {candles[-1].mts}]"


async def _diagnose(session: AsyncSession, cell: CellConfig, ticks: int) -> None:
    step = _TIMEFRAME_MS[cell.timeframe]
    budget = cell.staleness_budget_hours or 2

    latest = await get_up_to(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, mts_inclusive=2**62, lookback=1,
    )
    if not latest:
        logger.warning("%s: no final candles", cell.pair_id)
        return
    newest_final = latest[-1].mts

    # Boot at the tick that would have consumed `ticks` boundaries ago, mirroring a
    # daemon that started then and has been observing since.
    boot_mts = newest_final - ticks * step

    print(f"\n=== {cell.pair_id} (ema_span={cell.params.get('ema_span')}) ===")
    print(f"newest final candle: {newest_final}  boot: {boot_mts}  ticks: {ticks}")

    # ---- ARM A: warmup, exactly as warmup_cell does it -------------------------
    warm_history = await get_candles_in_range(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg,
        start_mts=boot_mts - _MR_LOOKBACK * step, end_mts=boot_mts,
    )
    warm = build_strategy_at_boundary(
        cell=cell, history=warm_history, ref_mts=boot_mts, budget_hours=budget,
    )
    print(
        f"  warmup     window={_span(warm_history)} observed={warm.observed_count} "
        f"ema={warm.strategy.ema_current}"
    )

    # ---- ARM B: replay at the same ref, as DivergenceReporter does it ----------
    rep_history = await get_up_to(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, mts_inclusive=boot_mts, lookback=_MR_LOOKBACK + 1,
    )
    rep = build_strategy_at_boundary(
        cell=cell, history=rep_history, ref_mts=boot_mts, budget_hours=budget,
    )
    print(
        f"  replay     window={_span(rep_history)} observed={rep.observed_count} "
        f"ema={rep.strategy.ema_current}"
    )
    print(
        f"  >>> gap at warmup time: {_rel(warm.strategy.ema_current, rep.strategy.ema_current)}"
    )

    # ---- Walk forward: live observes each boundary; replay rebuilds each time ---
    #
    # The first boundary a live daemon consumes is `boot_mts` ITSELF, not boot+step:
    # build_strategy_at_boundary drops the boundary slot, so warmup leaves state
    # observed only through boot-step, and the scheduler's first tick (at the next
    # wall-clock boundary) reads mts = boot. Starting the walk at boot+step would
    # skip one candle on the live arm and manufacture a gap that production does
    # not have — an earlier revision of this script did exactly that and reported
    # ~1.5e-2, three times the real figure.
    print(f"  (warmup left state observed through {boot_mts - step}; walk starts at {boot_mts})")
    live = warm.strategy
    for i in range(1, ticks + 1):
        boundary_mts = boot_mts + (i - 1) * step
        boundary = await get_up_to(
            session, symbol=cell.symbol, timeframe=cell.timeframe,
            period_agg=cell.period_agg, mts_inclusive=boundary_mts, lookback=1,
        )
        if not boundary or boundary[-1].mts != boundary_mts:
            print(f"  tick {i}: no final candle at {boundary_mts} — skipped")
            continue

        # live: incremental observe of this boundary (what process_candle does)
        ExtractedSignal.extract(cell, live, boundary[-1])

        # replay: full rebuild ending at this boundary
        hist = await get_up_to(
            session, symbol=cell.symbol, timeframe=cell.timeframe,
            period_agg=cell.period_agg, mts_inclusive=boundary_mts,
            lookback=_MR_LOOKBACK + 1,
        )
        rebuilt = build_strategy_at_boundary(
            cell=cell, history=hist, ref_mts=boundary_mts, budget_hours=budget,
        )
        replay_sig = ExtractedSignal.extract(cell, rebuilt.strategy, boundary[-1])

        print(
            f"  tick {i} @{boundary_mts}: live_ema={live.ema_current} "
            f"replay_ema={replay_sig.strategy_attributes and rebuilt.strategy.ema_current} "
            f"gap={_rel(live.ema_current, rebuilt.strategy.ema_current)} "
            f"(replay observed={rebuilt.observed_count})"
        )


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ticks", type=int, default=6)
    p.add_argument("--cells", default=str(DEFAULT_CELLS_YAML))
    args = p.parse_args()

    cells = load_cells_only(Path(args.cells))
    engine = make_engine(Settings())
    sf = make_session_factory(engine)
    try:
        async with session_scope(sf) as session:
            print(f"parity diagnosis @ {datetime.now(UTC).isoformat()}")
            for cell in cells:
                await _diagnose(session, cell, args.ticks)
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
