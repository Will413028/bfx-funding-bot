"""Bounded first-deployment reconcile; read-only venue, durable local evidence.

Requires an existing durable stop (trading state REDUCING or HALTED) and the
sole WriterLock. No policy seeding,
executor, permit, session worker, cancel, submit or daemon startup. Run from the
approved backend artifact with the restricted bot principal, then independently
review convert_capital_policy.py dry-run and explicitly apply its digest.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.crypto import load_kek
from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory
from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.nonce import make_monotonic_us_nonce
from bfx_funding_bot.modules.accounts.vault import load_account_credentials
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery, _AuthRestQuery
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from bfx_funding_bot.modules.marketfeed.config import canonical_cell_id


async def bootstrap_snapshot(*, database_url: str, account_id: UUID, environment: str,
    venue: _AuthRestQuery, credentials: Callable[[AsyncSession], Awaitable[Credentials]],
    clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    if environment not in {"prod", "ci"} or not 1 <= timeout_seconds <= 300:
        raise ValueError("bootstrap_scope_or_timeout_invalid")
    engine = make_async_engine_from_url(database_url)
    factory = make_session_factory(engine)
    writer = WriterLock(database_url=database_url, key=derive_lock_key(str(account_id), environment))
    trading = TradingStateRepository(factory, account_id=account_id, deployment_environment=environment)
    try:
        async with asyncio.timeout(timeout_seconds):
            await writer.acquire()
            epoch = await trading.current()
            if epoch is None or epoch.allows_new_offers:
                raise ValueError("bootstrap_existing_halt_required")
            async with factory() as session:
                credential = await credentials(session)
            repository = CapitalRepository(account_id=account_id, environment=environment,
                max_snapshot_age_ms=300000)
            recovery = BootRecovery(store=PostgresEventStore(deployment_environment=environment),
                session_factory=factory, auth_rest=venue,
                account_ctx=AccountContext(str(account_id), credential, Decimal("0")),
                deployment_environment=environment, bus=DomainEventBus(),
                symbols=["fUST", "fUSD"], max_attempts=1,
                capital_repository=repository, clock=clock)
            if not await writer.verify_held():
                raise ValueError("bootstrap_writer_lost")
            result = await recovery.run()
            # Validate canonical snapshot availability WITHOUT applying any policy.
            # Recovery can preserve an observation while declining capital acceptance.
            async with factory() as session:
                await repository.preview_policy(session, symbol="fUST",
                    cell_id=canonical_cell_id("fUST", "a30"),
                    now_ms=clock(), policy=CapitalPolicy(enabled=True))
            current = await trading.current()
            if (not await writer.verify_held() or current is None or current.id != epoch.id
                    or current.allows_new_offers):
                raise ValueError("bootstrap_writer_or_halt_changed")
            return {"status": "snapshot_ready", "account_id": str(account_id),
                "environment": environment, "trading_state_id": epoch.id,
                "snapshot_seq": result.snapshot_event_seq, "resumed": False,
                "policies_applied": False}
    finally:
        await writer.release()
        await engine.dispose()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    async def credentials(session: AsyncSession) -> Credentials:
        return await load_account_credentials(session, exchange_account_id=args.account_id, kek=load_kek())
    async with httpx.AsyncClient(timeout=15, trust_env=False) as http:
        return await bootstrap_snapshot(database_url=os.environ["DATABASE_URL"],
            account_id=args.account_id, environment=args.environment,
            venue=BitfinexAuthREST(http=http, nonce_provider=make_monotonic_us_nonce()),
            credentials=credentials, timeout_seconds=args.timeout_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=UUID, required=True)
    parser.add_argument("--environment", choices=("prod", "ci"), required=True)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args()
    logging.disable(sys.maxsize)
    try:
        print(json.dumps(asyncio.run(_run(args)), sort_keys=True))
        return 0
    except Exception:
        print('{"status":"blocked","reason":"bootstrap_failed","resumed":false}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
