"""Dry-run -> digest-confirmed amendment of an applied CapitalPolicy; never resume.

Sets ``enabled``, the per-offer ceiling (max_offer_amount) and the offer
envelope (periods, open offers, rate floor). Without an envelope the
offer-envelope guard refuses every offer for the symbol; the first envelope
needs all five envelope options.

  python -m scripts.amend_capital_policy --exchange-account-id UUID \\
    --environment prod --symbol fUST --max-offer-amount 200 \\
    --min-period-days 2 --max-period-days 2 --max-open-offers 6 \\
    --rate-floor-ratio 0.5 --min-rate-apr 0.01
  # review the complete report, then repeat with --apply-digest DIGEST
  # --reason "why this change": required to apply, recorded in the revision's source

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

from bfx_funding_bot.apps.bot_ports import build_policy_ports
from bfx_funding_bot.core.authority import require_ledger_authority
from bfx_funding_bot.core.database_realm import read_database_realm
from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory
from bfx_funding_bot.modules.accounts.capital_amendment import (
    PolicyChanges,
    amend_capital_policy,
)
from bfx_funding_bot.modules.ledger import PolicyRefused, Scope


async def run(args: argparse.Namespace) -> dict[str, Any]:
    engine = make_async_engine_from_url(os.environ["DATABASE_URL"])
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            # An unstamped or unknown realm refuses, and so does a database not on the ledger.
            await read_database_realm(session)
            await require_ledger_authority(session)
        policy = build_policy_ports(Scope(args.exchange_account_id, args.environment))
        async with factory() as session:
            report = await amend_capital_policy(
                session, store=policy.store, scope_lock=policy.scope_lock, symbol=args.symbol,
                changes=_changes(args), apply_digest=args.apply_digest,
                origin=None if args.reason is None else {"reason": args.reason})
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


def _enabled(value: str) -> bool:
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError("true or false")
    return value == "true"


def _changes(args: argparse.Namespace) -> PolicyChanges:
    return PolicyChanges(
        enabled=args.enabled, max_offer_amount=args.max_offer_amount,
        min_period_days=args.min_period_days, max_period_days=args.max_period_days,
        max_open_offers=args.max_open_offers, rate_floor_ratio=args.rate_floor_ratio,
        min_rate_apr=args.min_rate_apr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exchange-account-id", type=UUID, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--enabled", type=_enabled)
    parser.add_argument("--max-offer-amount", type=_amount)
    parser.add_argument("--min-period-days", type=int)
    parser.add_argument("--max-period-days", type=int)
    parser.add_argument("--max-open-offers", type=int)
    parser.add_argument("--rate-floor-ratio", type=_amount)
    parser.add_argument("--min-rate-apr", type=_amount, help="annual fraction, 0.01 = 1%%")
    parser.add_argument("--apply-digest")
    parser.add_argument("--reason", help="why: required with --apply-digest, kept in the revision")
    args = parser.parse_args()
    if args.apply_digest is not None and not (args.reason or "").strip():
        # No operator request stands behind an owner's revision: its source is the only place
        # the reason can live.
        parser.error("--apply-digest needs a non-blank --reason")
    logging.disable(sys.maxsize)
    try:
        print(json.dumps(asyncio.run(run(args)), sort_keys=True, indent=2))
        return 0
    except PolicyRefused as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # Never expose a connection URL, DB bind values or credentials.
        print('{"status":"blocked","reason":"amendment_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
