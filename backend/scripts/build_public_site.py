"""Build the public proof site's static files (ADR 2026-08-31 D6) into a directory.

bfx-public-site.service's one-shot build container (deploy/vm/ops/
docker-compose.public-site.yml, the weekly chain's image and env): reads
attribution_weekly and the hourly p2 funding candles with the research role, renders
modules/live_validation/public_site.py, and swaps the result into --out whole, so the
uploader (bfx_public_site.py publish) never sees half a build. Uploading is not this
script's job: it holds no Cloudflare token. A non-zero exit fails the unit.

The published account is the one the weekly chain measures (BFX_EXCHANGE_ACCOUNT_ID
from research.env; the runner refuses to start without it). The retired webapi
endpoint had its own BFX_PUBLIC_EXCHANGE_ACCOUNT_ID because the webapi serves every
account; attribution_weekly is computed for this one only.

Run from backend/ (env: DATABASE_URL / BFX_EXCHANGE_ACCOUNT_ID / BFX_DEPLOYMENT_ENV):
  uv run python -m scripts.build_public_site --out /tmp/public-site
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings, require_deployment_environment
from bfx_funding_bot.modules.accounts.exchange_accounts import account_id_canonical
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.live_validation.public_site import (
    CSV_SYMBOLS,
    aggregate_weeks,
    render_site,
)
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow


def write_site(out: Path, files: dict[str, bytes]) -> None:
    """Write next to `out`, then swap it in: the old build stays whole until the rename."""
    staging = out.with_name(f".{out.name}.new")
    previous = out.with_name(f".{out.name}.old")
    for path in (staging, previous):
        shutil.rmtree(path, ignore_errors=True)
    for name, body in files.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    if out.exists():
        out.rename(previous)
    staging.rename(out)
    shutil.rmtree(previous, ignore_errors=True)


async def _amain(args: argparse.Namespace) -> int:
    raw_account_id = os.environ.get("BFX_EXCHANGE_ACCOUNT_ID", "").strip()
    if not raw_account_id:
        raise RuntimeError("BFX_EXCHANGE_ACCOUNT_ID is required")
    account_id = account_id_canonical(raw_account_id)
    env = require_deployment_environment()
    engine = make_engine(Settings())
    try:
        async with make_session_factory(engine)() as session:
            weekly = list((await session.execute(
                select(AttributionWeeklyRow)
                .where(AttributionWeeklyRow.exchange_account_id == account_id,
                       AttributionWeeklyRow.deployment_environment == env)
            )).scalars())
            csv_rows = {}
            for symbol in CSV_SYMBOLS:
                rows = (await session.execute(
                    select(FundingCandleRow.mts, FundingCandleRow.close)
                    .where(FundingCandleRow.symbol == symbol, FundingCandleRow.timeframe == "1h",
                           FundingCandleRow.period_agg == "p2", FundingCandleRow.close.is_not(None))
                    .order_by(FundingCandleRow.mts)
                )).tuples().all()
                # An empty history is a broken read, not news: keep the last build.
                if not rows:
                    print(f"ERROR no 1h p2 candles for {symbol}; public site not rebuilt")
                    return 1
                csv_rows[symbol] = [(mts, close) for mts, close in rows if close is not None]
    finally:
        await engine.dispose()
    now = datetime.now(UTC)
    as_of = max((r.computed_at for r in weekly), default=now)
    weeks = aggregate_weeks(weekly)
    files = render_site(weeks, csv_rows, as_of=as_of, generated_at=now)
    write_site(args.out, files)
    print(f"public site built: {len(files)} files, {len(weeks)} weeks, "
          f"as of {as_of.isoformat()} -> {args.out}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="the site directory to replace")
    sys.exit(asyncio.run(_amain(parser.parse_args())))
