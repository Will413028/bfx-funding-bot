"""Ingest perp funding-rate history into perp_funding_rates (research-only).

Sources:
- Bitfinex /v2/status/deriv/{key}/hist — 1-min snapshots (CURRENT_FUNDING,
  NEXT_FUNDING_ACCRUED, prices, OI) since ~2019-08.
- Binance /fapi/v1/fundingRate — realized 8h funding since 2019-09 (optional).

Resume-aware + idempotent: per series it (a) tops up the gap between DB max
and now, then (b) walks back from DB min until the API returns empty. Safe to
interrupt and re-run at any point; commits every --pages-per-commit pages.
Does NOT touch the live trading path.

Usage:
    cd backend_py
    uv run python -m scripts.ingest_perp_funding
    uv run python -m scripts.ingest_perp_funding --symbols tBTCF0:USTF0 --skip-binance

Exit codes: 0 all series ok + PASS checks · 1 PASS check failed ·
2 a series errored · 3 operational error.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.external_signals.client import ExternalSignalsClient
from bfx_funding_bot.modules.external_signals.service import (
    IngestStats,
    backfill_perp_funding_to_earliest,
    topup_binance_funding_to_latest,
    topup_perp_funding_to_latest,
)
from bfx_funding_bot.modules.external_signals.tables import PerpFundingRateRow

logger = logging.getLogger("ingest_perp_funding")

DEFAULT_BFX_SYMBOLS = ["tBTCF0:USTF0", "tETHF0:USTF0"]
DEFAULT_BINANCE_SYMBOLS = ["BTCUSDT", "ETHUSDT"]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", default=",".join(DEFAULT_BFX_SYMBOLS),
                   help="Comma-separated Bitfinex deriv keys")
    p.add_argument("--binance-symbols", default=",".join(DEFAULT_BINANCE_SYMBOLS),
                   help="Comma-separated Binance USD-M symbols")
    p.add_argument("--skip-binance", action="store_true")
    p.add_argument("--skip-bitfinex", action="store_true")
    p.add_argument("--page-limit", type=int, default=5000)
    p.add_argument("--pages-per-commit", type=int, default=20,
                   help="Commit every N pages so interrupts lose little work")
    return p.parse_args()


def _fmt_mts(mts: int | None) -> str:
    if mts is None:
        return "N/A"
    return datetime.fromtimestamp(mts / 1000, UTC).strftime("%Y-%m-%d %H:%M")


async def _run_chunked(
    label: str,
    session_factory: async_sessionmaker[AsyncSession],
    run_chunk: Callable[[AsyncSession], Awaitable[IngestStats]],
) -> tuple[int, int]:
    """Run a service loop in pages-per-commit chunks until it reports done."""
    total_pages = 0
    total_rows = 0
    while True:
        async with session_scope(session_factory) as session:
            stats = await run_chunk(session)
        total_pages += stats.pages
        total_rows += stats.rows
        logger.info(
            "[%s] +%d pages (+%d rows) | total %d pages %d rows | range %s → %s%s",
            label, stats.pages, stats.rows, total_pages, total_rows,
            _fmt_mts(stats.earliest_mts), _fmt_mts(stats.latest_mts),
            " | DONE" if stats.done else "",
        )
        if stats.done:
            return total_pages, total_rows


async def _series_pass_check(
    session: AsyncSession, *, venue: str, symbol: str
) -> tuple[bool, str]:
    n = (
        await session.execute(
            select(func.count()).select_from(PerpFundingRateRow).where(
                PerpFundingRateRow.venue == venue,
                PerpFundingRateRow.symbol == symbol,
            )
        )
    ).scalar_one()
    if n == 0:
        return False, f"{venue}:{symbol} row_count == 0"
    lo, hi, max_abs = (
        await session.execute(
            select(
                func.min(PerpFundingRateRow.mts),
                func.max(PerpFundingRateRow.mts),
                func.max(func.abs(PerpFundingRateRow.funding_rate)),
            ).where(
                PerpFundingRateRow.venue == venue,
                PerpFundingRateRow.symbol == symbol,
            )
        )
    ).one()
    if max_abs is not None and max_abs > 0.05:
        return False, (
            f"{venue}:{symbol} max |funding_rate| = {max_abs} implausible (> 5%/period)"
        )
    return True, (
        f"{venue}:{symbol} rows={n} range {_fmt_mts(lo)} → {_fmt_mts(hi)} "
        f"max|rate|={max_abs}"
    )


async def _amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    bfx_symbols = (
        [] if args.skip_bitfinex
        else [s.strip() for s in args.symbols.split(",") if s.strip()]
    )
    binance_symbols = (
        [] if args.skip_binance
        else [s.strip() for s in args.binance_symbols.split(",") if s.strip()]
    )

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    series_errors: list[str] = []

    try:
        async with httpx.AsyncClient() as http:
            client = ExternalSignalsClient(http=http)

            def _mk_topup(sym: str) -> Callable[[AsyncSession], Awaitable[IngestStats]]:
                async def run(s: AsyncSession) -> IngestStats:
                    return await topup_perp_funding_to_latest(
                        client=client, session=s, symbol=sym,
                        page_limit=args.page_limit, max_pages=args.pages_per_commit,
                    )
                return run

            def _mk_walkback(sym: str) -> Callable[[AsyncSession], Awaitable[IngestStats]]:
                async def run(s: AsyncSession) -> IngestStats:
                    return await backfill_perp_funding_to_earliest(
                        client=client, session=s, symbol=sym,
                        page_limit=args.page_limit, max_pages=args.pages_per_commit,
                    )
                return run

            def _mk_binance(sym: str) -> Callable[[AsyncSession], Awaitable[IngestStats]]:
                async def run(s: AsyncSession) -> IngestStats:
                    return await topup_binance_funding_to_latest(
                        client=client, session=s, symbol=sym,
                        max_pages=args.pages_per_commit,
                    )
                return run

            for symbol in bfx_symbols:
                try:
                    logger.info("→ [bitfinex:%s] top-up to latest", symbol)
                    await _run_chunked(
                        f"bitfinex:{symbol}:topup", session_factory, _mk_topup(symbol)
                    )
                    logger.info("→ [bitfinex:%s] walk-back to earliest", symbol)
                    await _run_chunked(
                        f"bitfinex:{symbol}:walkback", session_factory,
                        _mk_walkback(symbol),
                    )
                except Exception as e:
                    logger.exception("series bitfinex:%s failed", symbol)
                    series_errors.append(f"bitfinex:{symbol}: {e!r}")

            for symbol in binance_symbols:
                try:
                    logger.info("→ [binance-usdm:%s] forward-fill", symbol)
                    await _run_chunked(
                        f"binance-usdm:{symbol}", session_factory, _mk_binance(symbol)
                    )
                except Exception as e:
                    logger.exception("series binance-usdm:%s failed", symbol)
                    series_errors.append(f"binance-usdm:{symbol}: {e!r}")

        # PASS checks
        print("\n=== PASS checks ===")
        all_pass = True
        async with session_scope(session_factory) as session:
            checks = [("bitfinex", s) for s in bfx_symbols] + [
                ("binance-usdm", s) for s in binance_symbols
            ]
            for venue, symbol in checks:
                ok, msg = await _series_pass_check(session, venue=venue, symbol=symbol)
                print(f"[{'✓' if ok else '✗'}] {msg}")
                all_pass = all_pass and ok

        for err in series_errors:
            print(f"[✗] series error: {err}")
        if not all_pass:
            return 1
        if series_errors:
            return 2
        return 0
    except Exception:
        logger.exception("ingest_perp_funding failed")
        return 3
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
