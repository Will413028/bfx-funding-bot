"""Dry-run -> digest-confirmed amendment of an applied CapitalPolicy; never resume.

Sets the absolute per-offer ceiling (max_offer_amount, T9). Without it the
max_offer_amount pre-trade guard refuses every offer for the symbol.

  python -m scripts.amend_capital_policy --exchange-account-id UUID \\
    --environment prod --symbol fUST --max-offer-amount 200
  # review the complete report, then repeat with --apply-digest DIGEST

On the VM run it as a one-shot of the deployed backend image with
/opt/bfx/runtime/migrate.env (the owner role: the runtime role may not write
policy). DATABASE_URL must be supplied explicitly; this command does not load .env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory
from bfx_funding_bot.modules.accounts.capital_amendment import amend_capital_policy
from bfx_funding_bot.modules.execution.capital_repository import (
    CapitalBlockedError,
    CapitalRepository,
)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    engine = make_async_engine_from_url(os.environ["DATABASE_URL"])
    try:
        repo = CapitalRepository(account_id=args.exchange_account_id, environment=args.environment,
                                 max_snapshot_age_ms=60_000)
        async with make_session_factory(engine)() as session:
            report = await amend_capital_policy(
                session, repository=repo, symbol=args.symbol,
                max_offer_amount=args.max_offer_amount, apply_digest=args.apply_digest)
            if report["status"] == "applied":
                await session.commit()
            else:
                await session.rollback()
            return report
    finally:
        await engine.dispose()


def _amount(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("not a decimal amount") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exchange-account-id", type=UUID, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--max-offer-amount", type=_amount, required=True)
    parser.add_argument("--apply-digest")
    args = parser.parse_args()
    logging.disable(sys.maxsize)
    try:
        print(json.dumps(asyncio.run(run(args)), sort_keys=True, indent=2))
        return 0
    except CapitalBlockedError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # Never expose a connection URL, DB bind values or credentials.
        print('{"status":"blocked","reason":"amendment_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
