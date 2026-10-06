"""Prepare a simulation database for the simulated venue (owner role; idempotent).

The target database must already be migrated and stamped realm ``shadow`` (the owner's
one-time stamp, docs/runbooks/fresh-host-setup.md). The stamp, and the realm trigger on every
table, are the single authority on what a database holds: this script refuses a database that
is unstamped or stamped any other realm, and checks nothing else about its content. Then, in
ONE transaction, it:

  1. refuses a database whose latest authority epoch is not ``ledger`` (the genesis migration
     puts a fresh database on the ledger; this script never writes an epoch);
  2. creates the simulation exchange account (the UUID you pass; the label is descriptive;
     NO credential row -- a simulated boot generates its own throwaway keys);
  3. writes the target CapitalPolicy of every symbol under the scope lock, a revision only
     where it differs from what is applied: --symbol policies are enabled, with the offer
     ceiling and all five envelope options (without them the envelope guard refuses every
     offer); --disabled-symbol policies are disabled. What may be enabled is the ledger's
     rule (``write_policy_revision``), not this script's;
  4. with ``--activate``, appends ``ACTIVE`` (cause ``operator``, actor ``bootstrap_simulation_db``)
     to ``trading_state`` ONLY when the scope has no state row at all. A fresh scope is HALTED
     until someone says otherwise, so a bootstrapped database that never trades makes a soak
     vacuous. Any existing row (a HALT above all) is left alone: scripts never resume.

A second run changes nothing. Run as a one-shot of the deployed backend image with the
owner role; DATABASE_URL must be supplied explicitly (this command does not load .env):

  python -m scripts.bootstrap_simulation_db --exchange-account-id UUID \\
    --symbol fUST --disabled-symbol fUSD --max-offer-amount 200 \\
    --min-period-days 2 --max-period-days 2 --max-open-offers 6 \\
    --rate-floor-ratio 0.5 --min-rate-apr 0.01 --activate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.apps.bot_ports import select_policy_ports
from bfx_funding_bot.core.authority import AUTHORITY_TABLE
from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch, read_database_realm
from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory
from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.execution.safety.trading_state import (
    ACTIVE,
    CAUSE_OPERATOR,
    append_transition,
    read_current,
)
from bfx_funding_bot.modules.ledger import PolicyRefused, Scope
from bfx_funding_bot.modules.trading import (
    CapitalPolicy,
    OfferEnvelope,
    policy_digest,
    policy_payload,
)

ALLOWED_REALMS = ("shadow",)
SIMULATION_LABEL = "simulation"  # descriptive only: nothing checks it
_ACTOR = "bootstrap_simulation_db"


class BootstrapRefused(RuntimeError):  # noqa: N818 - a refusal, carrying its reason code
    """The database is not one a simulation may be prepared in."""


async def _refuse_unless_simulation_database(
    session: AsyncSession, allowed_realms: tuple[str, ...],
) -> str:
    realm = await read_database_realm(session)  # unstamped / unknown refuses
    if realm not in allowed_realms:
        raise BootstrapRefused(f"database_realm_not_simulation stamp={realm}")
    return realm


async def _refuse_unless_ledger(session: AsyncSession) -> None:
    latest = await session.scalar(text(
        f"SELECT authority FROM {AUTHORITY_TABLE} ORDER BY epoch_seq DESC LIMIT 1"))
    if latest != "ledger":
        raise BootstrapRefused(f"authority_epoch_not_ledger latest={latest}")


async def _ensure_account(session: AsyncSession, account_id: UUID) -> bool:
    credentials = await session.scalar(select(ExchangeAccountCredential.exchange_account_id).where(
        ExchangeAccountCredential.exchange_account_id == account_id))
    if credentials is not None:
        raise BootstrapRefused("simulation_account_has_credentials")
    existing = await session.get(ExchangeAccount, account_id)
    if existing is not None:
        if existing.lifecycle_status != "active":
            raise BootstrapRefused("simulation_account_is_not_active")
        return False
    session.add(ExchangeAccount(
        id=account_id, venue="bitfinex", label=SIMULATION_LABEL, lifecycle_status="active"))
    await session.flush()
    return True


async def _ensure_active(session: AsyncSession, *, scope: Scope, now_ms: int) -> str:
    """Append the first ``ACTIVE`` row through the domain writer; "kept_*" when the scope
    already has any state row (a HALT above all: scripts never resume)."""
    await acquire_transaction_lock(
        session, account_id=str(scope.exchange_account_id),
        deployment_environment=scope.deployment_environment)
    current = await read_current(
        session, account_id=scope.exchange_account_id, environment=scope.deployment_environment)
    if current is not None:
        return f"kept_{current.state.lower()}"
    await append_transition(
        session, account_id=scope.exchange_account_id, environment=scope.deployment_environment,
        state=ACTIVE, cause=CAUSE_OPERATOR, actor=_ACTOR, reason="simulation bootstrap",
        now_ms=now_ms)
    return "activated"


async def _ensure_policy(
    session: AsyncSession, *, scope: Scope, symbol: str, target: CapitalPolicy,
) -> str:
    """Write ``target`` as the next revision unless it is already what is applied."""
    policy = select_policy_ports(scope)
    await policy.scope_lock.lock(session, scope)
    try:
        applied = await policy.store.read_applied(session, symbol=symbol)
    except PolicyRefused:
        applied = None  # no policy yet: the first revision
    if applied is not None and applied.digest == policy_digest(policy_payload(target)):
        return "unchanged"
    await policy.store.apply_policy(
        session, symbol=symbol, policy=target,
        expected_revision=applied.revision if applied is not None else 0,
        source={"actor": _ACTOR, "bootstrap": True})
    return "applied"


async def run(
    args: argparse.Namespace, *, database_url: str | None = None,
    allowed_realms: tuple[str, ...] = ALLOWED_REALMS,
) -> dict[str, Any]:
    """Prepare the database; ``allowed_realms`` is widened to ``ci`` only by tests."""
    engine = make_async_engine_from_url(database_url or os.environ["DATABASE_URL"])
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            try:
                realm = await _refuse_unless_simulation_database(session, allowed_realms)
                await _refuse_unless_ledger(session)
                account_created = await _ensure_account(session, args.exchange_account_id)
                enabled = CapitalPolicy(
                    enabled=True, max_offer_amount=args.max_offer_amount,
                    envelope=OfferEnvelope(
                        min_period_days=args.min_period_days,
                        max_period_days=args.max_period_days,
                        max_open_offers=args.max_open_offers,
                        rate_floor_ratio=args.rate_floor_ratio, min_rate_apr=args.min_rate_apr))
                targets = dict.fromkeys(args.symbol, enabled)
                targets |= {symbol: CapitalPolicy(enabled=False) for symbol in args.disabled_symbol}
                scope = Scope(args.exchange_account_id, realm)
                policies = {
                    symbol: await _ensure_policy(
                        session, scope=scope, symbol=symbol, target=target)
                    for symbol, target in sorted(targets.items())
                }
                trading = (
                    await _ensure_active(session, scope=scope, now_ms=int(time.time() * 1000))
                    if args.activate else "not_requested")
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
        return {
            "status": "ready", "realm": realm, "account_id": str(args.exchange_account_id),
            "account_created": account_created,
            "policies": policies, "trading_state": trading,
        }
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
    parser.add_argument("--symbol", action="append", default=[],
                        help="a symbol whose policy is enabled with the values below")
    parser.add_argument("--disabled-symbol", action="append", default=[],
                        help="a symbol that gets a disabled policy (the ledger never enables fUSD)")
    parser.add_argument("--max-offer-amount", type=_amount, required=True)
    parser.add_argument("--min-period-days", type=int, required=True)
    parser.add_argument("--max-period-days", type=int, required=True)
    parser.add_argument("--max-open-offers", type=int, required=True)
    parser.add_argument("--rate-floor-ratio", type=_amount, required=True)
    parser.add_argument("--min-rate-apr", type=_amount, required=True,
                        help="annual fraction, 0.01 = 1%%")
    parser.add_argument("--activate", action="store_true",
                        help="append ACTIVE when the scope has no trading_state row yet "
                             "(never overrides an existing HALT)")
    args = parser.parse_args()
    if not args.symbol and not args.disabled_symbol:
        parser.error("at least one --symbol or --disabled-symbol is required")
    logging.disable(sys.maxsize)
    try:
        print(json.dumps(asyncio.run(run(args)), sort_keys=True, indent=2))
        return 0
    except (BootstrapRefused, PolicyRefused, DatabaseRealmMismatch) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # Never expose a connection URL, DB bind values or credentials.
        print('{"status":"blocked","reason":"bootstrap_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
