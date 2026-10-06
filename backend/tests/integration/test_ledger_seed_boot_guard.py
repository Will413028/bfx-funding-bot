"""``require_ledger_seed`` (H-1) on clone PostgreSQL: a scope with legacy history boots the
real venue only over its ``legacy_seed`` observation; without history no seed is needed.

Legacy history is a frozen ``event_log`` row of the scope's exchange account in its
environment. (The database holds ``event_log.exchange_account_id`` NOT NULL since the
identity contract, ``9b2c3d4e5f6a``: no row is unattributed.)

Mutation checks (one at a time; revert after each):

* Return ``True`` from ``has_legacy_history`` (the guard keyed on nothing but the seed again):
  ``test_a_scope_without_legacy_history_needs_no_seed`` and
  ``test_history_of_another_scope_or_environment_is_not_the_scopes`` fail.
* Drop the exchange-account filter of ``has_legacy_history``:
  ``test_history_of_another_scope_or_environment_is_not_the_scopes`` fails.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bfx_funding_bot.apps.authority_support import has_legacy_history, require_ledger_seed
from bfx_funding_bot.core.authority import AuthorityMismatch
from bfx_funding_bot.core.venue import Venue
from bfx_funding_bot.modules.ledger import Scope
from tests.pg_templates import disable_realm_triggers

from .test_ledger_schema_roles import _A, _build, _seed
from .test_ledger_seed_schema import _owner_seed_observation

pytestmark = pytest.mark.integration

SCOPE = Scope(UUID(_A), "ci")
OTHER = "00000000-0000-0000-0000-00000000b001"


@pytest.fixture
def db(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("ledger_s1_roles", _build))
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed(conn)  # the scope's exchange account and one row per ledger table
        conn.exec_driver_sql(
            f"INSERT INTO exchange_accounts(id,venue,label) VALUES ('{OTHER}','bitfinex','other')")
    try:
        yield engine
    finally:
        engine.dispose()


def _event(engine: Engine, account: str, environment: str = "ci") -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO event_log(account_id, exchange_account_id, deployment_environment, "
            f"event_type, payload, occurred_at_ms) VALUES ('{account}', '{account}', "
            f"'{environment}', 'x', '{{}}', 1)")


def _seeded(engine: Engine) -> None:
    with engine.begin() as conn:
        _owner_seed_observation(conn)


def _guard(engine: Engine, *, venue: Venue = "bitfinex") -> bool:
    """Run the guard for ``SCOPE``; True when it lets the boot through."""
    url = engine.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

    async def run() -> bool:
        async_engine = create_async_engine(url)
        try:
            async with AsyncSession(async_engine) as session:
                try:
                    await require_ledger_seed(session, venue=venue, scopes=(SCOPE,))
                except AuthorityMismatch as exc:
                    assert str(exc) == f"ledger_seed_missing scope={_A}:ci"
                    return False
                return True
        finally:
            await async_engine.dispose()

    return asyncio.run(run())


def _history(engine: Engine) -> bool:
    url = engine.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

    async def run() -> bool:
        async_engine = create_async_engine(url)
        try:
            async with AsyncSession(async_engine) as session:
                return await has_legacy_history(session, SCOPE)
        finally:
            await async_engine.dispose()

    return asyncio.run(run())


def test_legacy_history_without_the_seed_refuses_and_with_it_boots(db) -> None:
    _event(db, _A)
    assert _history(db) is True
    assert _guard(db) is False
    _seeded(db)
    assert _guard(db) is True


def test_a_scope_without_legacy_history_needs_no_seed(db) -> None:
    """A fresh host: whatever rests at the venue is foreign to the ledger."""
    assert _history(db) is False
    assert _guard(db) is True


def test_history_of_another_scope_or_environment_is_not_the_scopes(db) -> None:
    _event(db, OTHER)
    disable_realm_triggers(db)  # plant a row of another realm on purpose, on this clone
    _event(db, _A, "prod")
    assert _history(db) is False
    assert _guard(db) is True


def test_the_simulated_venue_never_needs_the_seed(db) -> None:
    _event(db, _A)
    assert _guard(db, venue="simulated") is True
