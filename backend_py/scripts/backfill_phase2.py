"""Phase 2 orchestrator: 8 series sequential backfill + 4 PASS checks.

Usage:
    cd backend_py
    uv run python scripts/backfill_phase2.py                # real run
    uv run python scripts/backfill_phase2.py --dry-run      # no API / no DB write

Series matrix (hardcoded):
    candles fUSD 1h p2 / p30 / a30
    candles fUST 1h p2 / p30 / a30
    funding_stats fUSD
    funding_stats fUST

Exit codes:
    0 — Phase 2 PASS (all 4 checks pass, all 8 series populated)
    1 — At least one PASS check failed
    2 — At least one series raised during backfill (others may have succeeded)
    3 — Operational error (network, DB connection, settings)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import (
    make_engine,
    make_session_factory,
    session_scope,
)
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.backfill.checks import (
    check_continuity,
    check_frr_unit_stability,
    check_round_trip,
    check_row_counts,
)
from bfx_funding_bot.modules.backfill.schemas import (
    BackfillError,
    BackfillStats,
    SeriesSpec,
)
from bfx_funding_bot.modules.candles.service import backfill_candles_to_earliest
from bfx_funding_bot.modules.funding_stats.service import (
    backfill_funding_stats_to_earliest,
)

logger = logging.getLogger("backfill_phase2")

SERIES_MATRIX: list[SeriesSpec] = [
    SeriesSpec(kind="candles", symbol="fUSD", timeframe="1h", period_agg="p2"),
    SeriesSpec(kind="candles", symbol="fUSD", timeframe="1h", period_agg="p30"),
    SeriesSpec(kind="candles", symbol="fUSD", timeframe="1h", period_agg="a30"),
    SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg="p2"),
    SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg="p30"),
    SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg="a30"),
    SeriesSpec(kind="funding_stats", symbol="fUSD"),
    SeriesSpec(kind="funding_stats", symbol="fUST"),
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run", action="store_true",
        help="Skip API calls and DB writes; verify settings + connectivity only.",
    )
    return p.parse_args()


async def _backfill_one(
    spec: SeriesSpec,
    client: BitfinexREST,
    session_factory: async_sessionmaker[AsyncSession],
) -> BackfillStats | BackfillError:
    """Run one series in its own session_scope; catch + record any exception."""
    try:
        async with session_scope(session_factory) as session:
            if spec.kind == "candles":
                if spec.timeframe is None or spec.period_agg is None:
                    raise ValueError(
                        f"candles SeriesSpec missing timeframe/period_agg: {spec!r}"
                    )
                return await backfill_candles_to_earliest(
                    client=client, session=session,
                    symbol=spec.symbol,
                    timeframe=spec.timeframe,
                    period_agg=spec.period_agg,
                )
            return await backfill_funding_stats_to_earliest(
                client=client, session=session, symbol=spec.symbol,
            )
    except Exception as e:
        logger.exception("series %s failed", spec.label())
        return BackfillError(spec=spec, error=repr(e))


def _format_summary(results: list[BackfillStats | BackfillError]) -> str:
    lines = [
        "=== Phase 2 Backfill Summary ===",
        f"{'SERIES':<32} {'PAGES':>6} {'ROWS':>8} {'EARLIEST_MTS':>16} STATUS",
    ]
    for r in results:
        if isinstance(r, BackfillStats):
            lines.append(
                f"{r.spec.label():<32} {r.pages:>6} {r.rows:>8} "
                f"{r.earliest_mts:>16} ✓"
            )
        else:
            lines.append(f"{r.spec.label():<32} {'-':>6} {'-':>8} {'-':>16} ✗ {r.error}")
    return "\n".join(lines)


async def _amain() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    if args.dry_run:
        async with session_scope(session_factory) as session:
            await session.execute(text("SELECT 1"))
        logger.info("✅ Dry-run PASS: settings + DB connectivity OK")
        await engine.dispose()
        return 0

    started = time.time()
    try:
        async with httpx.AsyncClient() as http:
            client = BitfinexREST(
                http=http,
                base_url=settings.bitfinex_api_base_url,
                limiter=FundingRateLimiter(),
            )
            results: list[BackfillStats | BackfillError] = []
            for spec in SERIES_MATRIX:
                logger.info("→ starting %s", spec.label())
                t0 = time.time()
                r = await _backfill_one(spec, client, session_factory)
                dt = time.time() - t0
                if isinstance(r, BackfillStats):
                    logger.info(
                        "← %s done: %d pages, %d rows in %.1fs",
                        spec.label(), r.pages, r.rows, dt,
                    )
                else:
                    logger.error("← %s FAILED: %s", spec.label(), r.error)
                results.append(r)

            print(_format_summary(results))
            print()

            # Run 4 PASS checks
            specs_ok = [r.spec for r in results if isinstance(r, BackfillStats)]

            async with session_scope(session_factory) as session:
                rc = await check_row_counts(session, [r.spec for r in results])
                rt = await check_round_trip(session, client, specs_ok)
                fr = await check_frr_unit_stability(session, symbol="fUSD")
                ct = await check_continuity(session, specs_ok)

            print("=== PASS Checks ===")
            for label, res in [
                ("row_count > 0", rc),
                ("round-trip exact-match", rt),
                ("FRR unit stability (diagnostic)", fr),
                ("continuity", ct),
            ]:
                mark = "✓" if res.passed else "✗"
                print(f"[{mark}] {label}: {res.message}")
                for f in res.failures:
                    print(f"    - {f}")

            logger.info("Total elapsed: %.1fs", time.time() - started)

            checks_passed = rc.passed and rt.passed and fr.passed and ct.passed
            any_series_failed = any(isinstance(r, BackfillError) for r in results)

            if not checks_passed:
                print("\n❌ Phase 2 FAIL — see above")
                return 1
            if any_series_failed:
                print("\n⚠️ Phase 2 partial — checks pass but some series errored")
                return 2
            print("\n✅ Phase 2 PASS")
            return 0
    except Exception:
        logger.exception("Operational error during Phase 2 backfill")
        return 3
    finally:
        await engine.dispose()


def main() -> None:
    rc = asyncio.run(_amain())
    sys.exit(rc)


if __name__ == "__main__":
    main()
