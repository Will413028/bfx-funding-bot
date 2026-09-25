"""Read-only deployment receipts. No policy seed, migration, halt or venue call."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.release_session import ReleaseSessions
from bfx_funding_bot.modules.execution.release_worker import RELEASE_SCHEMA_HEAD


async def deployment_readiness(factory: async_sessionmaker[AsyncSession], *, account_id: UUID,
                               environment: str, expected_principal: str = "bfx_bot") -> dict[str, Any]:
    async with factory() as session:
        principal = await session.scalar(text("SELECT current_user"))
        if principal != expected_principal:
            raise ValueError("runtime_principal_mismatch")
        heads = list(await session.scalars(text("SELECT version_num FROM alembic_version")))
        if heads != [RELEASE_SCHEMA_HEAD]:
            raise ValueError("release_schema_mismatch")
        state = await ReleaseSessions(account_id, environment).trading_state(session)
        if state is None or state.allows_new_offers:
            raise ValueError("deployment_requires_existing_halt")
        repository = CapitalRepository(account_id=account_id, environment=environment, max_snapshot_age_ms=300000)
        policies = {}
        for symbol in ("fUST", "fUSD"):
            applied = await repository.read_applied(session, symbol=symbol)
            policy = applied.policy
            if (policy.enabled != (symbol == "fUST") or policy.reserve_amount != 0
                or policy.max_cell_fraction != Decimal("0.70")):
                raise ValueError("release_policy_mismatch")
            policies[symbol] = {"revision": applied.revision, "digest": applied.digest, "enabled": policy.enabled}
        return {"schema_head": RELEASE_SCHEMA_HEAD, "principal": principal, "account_id": str(account_id),
            "environment": environment, "trading_state_id": state.id,
            "trading_state": state.state, "halted": True, "policies": policies}


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    engine = make_async_engine_from_url(os.environ["DATABASE_URL"])
    factory = make_session_factory(engine)
    try:
        if args.mode == "schema":
            async with factory() as session:
                exists = await session.scalar(text("SELECT to_regclass('public.alembic_version')"))
                heads = list(await session.scalars(text("SELECT version_num FROM alembic_version"))) if exists else []
                return {"schema_heads": heads,
                    "database": await session.scalar(text("SELECT current_database()")),
                    "system_identifier": str(await session.scalar(text("SELECT system_identifier FROM pg_control_system()"))),
                    "target_head": RELEASE_SCHEMA_HEAD, "resumed": False}
        async with factory() as session:
            dangerous = await session.scalar(text("""SELECT
                (SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=current_user)
                OR EXISTS (SELECT 1 FROM pg_class WHERE relname IN ('capital_policy_heads','release_sessions')
                           AND pg_has_role(current_user, relowner, 'USAGE'))"""))
            if dangerous:
                raise ValueError("runtime_owner_or_superuser")
        return await deployment_readiness(factory, account_id=UUID(os.environ["BFX_EXCHANGE_ACCOUNT_ID"]),
            environment=os.environ["BFX_DEPLOYMENT_ENV"])
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("schema", "startup"))
    args = parser.parse_args()
    logging.disable(sys.maxsize)
    try:
        print(json.dumps(asyncio.run(_run(args)), sort_keys=True))
        return 0
    except Exception:
        print('{"status":"blocked","reason":"deployment_database_check_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
