"""Realized interest per calendar week, from the venue ledger (account level).

Read-only over funding_interest_payments (written by the bot's
InterestLedgerSync). Net of the venue fee, on the whole funding wallet: idle
capital counts, and early repayment needs no modelling.

Run from backend/ (env: DATABASE_URL / BFX_EXCHANGE_ACCOUNT_ID):
  uv run python -m scripts.report_interest [--weeks 8] [--currency UST] [--out FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    MS_PER_DAY,
    InterestSummary,
    summarize_interest,
)
from bfx_funding_bot.modules.live_validation.tables import FundingInterestPaymentRow
from bfx_funding_bot.modules.live_validation.weekly_attribution import calendar_week_start


def render(summaries: list[InterestSummary]) -> str:
    lines = ["| week (UTC) | payouts | net interest | mean wallet | net APR |",
             "|---|---:|---:|---:|---:|"]
    for s in summaries:
        week = time.strftime("%Y-%m-%d", time.gmtime(s.start_ms / 1000))
        balance = f"{s.mean_balance:.2f}" if s.mean_balance is not None else "—"
        apr = f"{s.net_apr_pct:.2f}%" if s.net_apr_pct is not None else "—"
        lines.append(f"| {week} | {s.payouts} | {s.net_interest:.6f} | {balance} | {apr} |")
    return "\n".join(lines) + "\n"


def weekly_summaries(payments: list[InterestPayment], *, currency: str, now_ms: int,
                     weeks: int) -> list[InterestSummary]:
    """The last `weeks` complete calendar weeks, oldest first."""
    this_week = calendar_week_start(now_ms)
    starts = [this_week - (i + 1) * 7 * MS_PER_DAY for i in reversed(range(weeks))]
    return [summarize_interest(payments, currency=currency, start_ms=start,
                               end_ms=start + 7 * MS_PER_DAY) for start in starts]


async def _amain(args: argparse.Namespace) -> int:
    raw_account = os.environ.get("BFX_EXCHANGE_ACCOUNT_ID", "").strip()
    if not raw_account:
        raise RuntimeError("BFX_EXCHANGE_ACCOUNT_ID is required")
    engine = make_engine(Settings())
    try:
        async with make_session_factory(engine)() as session:
            rows = (await session.scalars(select(FundingInterestPaymentRow).where(
                FundingInterestPaymentRow.exchange_account_id == UUID(raw_account),
                FundingInterestPaymentRow.currency == args.currency,
            ))).all()
    finally:
        await engine.dispose()
    payments = [InterestPayment(r.ledger_id, r.currency, None, r.mts, r.amount, r.balance,
                                r.description) for r in rows]
    report = render(weekly_summaries(payments, currency=args.currency,
                                     now_ms=int(time.time() * 1000), weeks=args.weeks))
    if args.out:
        Path(args.out).write_text(f"# Realized interest ({args.currency}, venue ledger)\n\n{report}")
    print(report, end="")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--currency", default="UST")
    parser.add_argument("--out")
    sys.exit(asyncio.run(_amain(parser.parse_args())))
