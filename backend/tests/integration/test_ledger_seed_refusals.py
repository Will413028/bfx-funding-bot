"""The seed command's refusals that need no legacy history, on a migrated database.

The legacy runtime that built a closure to seed is gone (S1-8), so what is left here is what
refuses before a closure is read: missing ``--authorize-seed``, a DSN that is not the table
owner, a realm or scope mismatch, and a database already on the ledger (a database migrated
to head starts on the genesis ``ledger`` epoch). Each refusal leaves every ledger table
empty and releases the seed's locks. The seed itself, its closure cases and its digest
verification ran in prod on 2026-10-05; the scaffolding goes in S1-8 PR-D.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from bfx_funding_bot.apps import ledger_seed as seed_app
from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES

from .bot_e2e import (
    SCOPE,
    T0,
    BotEnv,
    bot_env,  # noqa: F401 - fixture
    ledger_db,  # noqa: F401 - fixture dependency
)
from .seed_e2e import pre_switch, run_seed, seed_command, table_count

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SEED_AT = T0 + 100_000


async def assert_ledger_empty(env: BotEnv) -> None:
    for table in LEDGER_TABLES:
        assert await table_count(env, table.name) == 0, table.name


async def refused(env: BotEnv, argv: list[str], reason: str, **kwargs: Any) -> dict[str, Any]:
    code, lines = await run_seed(argv, now_ms=SEED_AT, **kwargs)
    assert code == seed_app.EXIT_REFUSED, lines
    summary = lines[-1]
    assert (summary["reason"], summary["committed"]) == (reason, False), summary
    await assert_ledger_empty(env)
    return summary


class Logins:
    """LOGIN roles made for one test on its own database server, dropped afterwards."""

    def __init__(self, url: Any) -> None:
        self.url = url
        self.made: list[str] = []

    def exec(self, sql: str) -> None:
        engine = create_engine(self.url)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(sql)
        finally:
            engine.dispose()

    def member_of(self, role: str) -> str:
        name = "seed_" + uuid4().hex[:12]
        self.made.append(name)
        self.exec(f"CREATE ROLE \"{name}\" LOGIN PASSWORD 'x' NOSUPERUSER")
        self.exec(f'GRANT {role} TO "{name}"')
        return name


@pytest.fixture
def logins(ledger_db: Any) -> Any:  # noqa: F811
    made = Logins(ledger_db.url)
    yield made
    for name in made.made:
        made.exec(f'DROP ROLE IF EXISTS "{name}"')


async def test_guards_refuse_without_authorization_owner_or_matching_realm(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, logins: Logins,  # noqa: F811
) -> None:
    env = bot_env
    await pre_switch(env)
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url, authorize=False),
                  "seed_authorization_required")
    runtime = logins.member_of("bfx_bot")
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url.set(username=runtime,
                                                                         password="x")),
                  "dsn_not_owner")
    prod = f"{SCOPE.exchange_account_id}:prod"
    argv = seed_command(env, tmp_path, url=ledger_db.url, realm="prod", run_id="seed-prod",
                        scopes=(prod,))
    manifest = argv[argv.index("--manifest") + 1]
    with open(manifest) as handle:
        body = handle.read().replace(":ci\"", ":prod\"")
    with open(manifest, "w") as handle:
        handle.write(body)
    await refused(env, argv, "realm_mismatch")
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url, run_id="seed-scope",
                                    scopes=(f"{uuid4()}:ci",)), "scope_mismatch")


async def test_a_database_on_the_ledger_refuses_the_seed(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """A fresh database starts on the ledger (genesis): there is nothing to seed."""
    env = bot_env
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url), "epoch_not_legacy")


def _trading_control_insert(request_id: UUID) -> Any:
    return text(
        "INSERT INTO trading_control_requests (request_id, exchange_account_id, "
        "deployment_environment, action, reason, requested_by, created_at_ms) "
        "VALUES (:r, :a, 'ci', 'kill', 'during the seed', 'operator', 1)"
    ).bindparams(r=request_id, a=SCOPE.exchange_account_id)



async def test_lock_table_takes_no_snapshot(ledger_db: Any) -> None:  # noqa: F811
    """The premise of the ordering above, on this PostgreSQL: a REPEATABLE READ transaction
    that has only run ``SET LOCAL`` and ``LOCK TABLE`` still sees a row committed after them;
    its first read fixes the snapshot."""
    url = ledger_db.url.set(drivername="postgresql+asyncpg")
    seeder, writer = create_async_engine(url), create_async_engine(url)
    insert = text("INSERT INTO exchange_accounts (id, venue, label) VALUES (:i, 'bitfinex', 'x')")
    count = text("SELECT count(*) FROM exchange_accounts WHERE id = :i")
    before, after = uuid4(), uuid4()
    try:
        async with seeder.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            await conn.begin()
            await conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            await conn.execute(text(
                "LOCK TABLE public.trading_control_requests IN SHARE MODE"))
            async with writer.begin() as other:
                await other.execute(insert, {"i": before})
            assert await conn.scalar(count, {"i": before}) == 1  # no snapshot until now
            async with writer.begin() as other:
                await other.execute(insert, {"i": after})
            assert await conn.scalar(count, {"i": after}) == 0  # the first read fixed it
            await conn.rollback()
    finally:
        await seeder.dispose()
        await writer.dispose()


async def test_a_refusal_releases_the_writer_and_request_locks(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """R1-2/R2-1 on the refusal path: a refusal inside the transaction (the genesis
    ``ledger`` epoch, after both the writer locks and the request lock were taken) leaves the
    writer keys and the request tables free."""
    env = bot_env
    argv = seed_command(env, tmp_path, url=ledger_db.url)
    await refused(env, argv, "epoch_not_legacy")
    other = create_async_engine(ledger_db.url.set(drivername="postgresql+asyncpg"))
    try:
        async with other.connect() as conn:
            await conn.execute(text("SET lock_timeout = '100ms'"))
            for key in seed_app._lock_keys(SCOPE):
                assert await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
                await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            await conn.execute(_trading_control_insert(uuid4()))
            await conn.commit()
            held = await conn.scalar(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid <> pg_backend_pid()"))
        assert held == 0
    finally:
        await other.dispose()
