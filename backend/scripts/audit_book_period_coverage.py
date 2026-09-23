"""Gate A0 — audit exact-period ask coverage in self-recorded funding books.

Read-only over `funding_book_snapshots` (no account scope: the table is a
public-market observation). Answers ADR 2026-06-04 Amendment's A0 question:
do period 7 / 14 ask levels exist on the fUST/fUSD book often enough for
AdaptivePeriod's mid/long tiers to pass exact-period eligibility?

Run from backend/ (env: DATABASE_URL):
  uv run python -m scripts.audit_book_period_coverage [--symbols fUST,fUSD]
      [--since-ms N] [--periods 2,7,14,30,120] [--out docs/research/<date>-a0-book-period-coverage.md]

VM (one-shot container, research clone mounted read-only, live bot untouched —
same pattern as /strategy-research):
  docker run --rm --label autoheal=false --network bfx_default \
    --env-file ~/bfx-funding-bot/.env.runtime \
    -v ~/bfx-research/backend/src:/app/src:ro -v ~/bfx-research/backend/scripts:/app/scripts:ro \
    bfx-bot:local python -m scripts.audit_book_period_coverage
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.marketfeed.book_period_coverage import (
    DEFAULT_PERIODS,
    BookAskSnapshot,
    render_markdown,
    snapshot_from_payload,
    summarize_period_coverage,
)
from bfx_funding_bot.modules.marketfeed.tables import FundingBookSnapshotRow


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--symbols", default="fUST,fUSD")
    p.add_argument("--since-ms", type=int, default=None)
    p.add_argument("--periods", default=",".join(str(x) for x in DEFAULT_PERIODS))
    p.add_argument("--out", type=Path, default=None)
    return p.parse_args(argv)


async def _load(symbols: list[str], since_ms: int | None) -> list[BookAskSnapshot]:
    engine = make_engine(Settings())
    sf = make_session_factory(engine)
    try:
        async with sf() as session:
            stmt = select(
                FundingBookSnapshotRow.symbol,
                FundingBookSnapshotRow.captured_at_ms,
                FundingBookSnapshotRow.payload,
            ).where(FundingBookSnapshotRow.symbol.in_(symbols))
            if since_ms is not None:
                stmt = stmt.where(FundingBookSnapshotRow.captured_at_ms >= since_ms)
            rows = (await session.execute(stmt.order_by(FundingBookSnapshotRow.captured_at_ms))).all()
    finally:
        await engine.dispose()
    return [
        snapshot_from_payload(symbol=symbol, captured_at_ms=ts, payload=payload)
        for symbol, ts, payload in rows
    ]


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    periods = tuple(int(x) for x in args.periods.split(",") if x.strip())
    snapshots = await _load(symbols, args.since_ms)
    report = render_markdown(summarize_period_coverage(snapshots, periods=periods))
    if not snapshots:
        report += "\n_no snapshots matched — check BFX_BOOK_SNAPSHOT_ENABLED and the symbol list_\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
        print(f"wrote {args.out}")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain(sys.argv[1:])))
