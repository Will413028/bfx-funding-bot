"""``require_ledger_epoch`` on clone PostgreSQL: the real venue boots on the latest ``ledger``
epoch only when the genesis migration or the S1-7 switch wrote it; any other writer refuses.
It reads the epoch only: no scope, no seed observation, never the frozen legacy tables.

Mutation checks (one at a time; revert after each):

* Let an unknown writer through (drop the writer refusal):
  ``test_an_epoch_written_by_anyone_else_refuses`` fails.
* Require a scope's ``legacy_seed`` observation after a switch epoch again (the guard this one
  replaced): ``test_a_switch_epoch_boots_an_account_the_switch_never_saw`` fails.
* Exempt the real venue from the authority check: ``test_a_legacy_epoch_refuses`` fails.
"""
from __future__ import annotations

import asyncio
import importlib.util

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bfx_funding_bot.apps.authority_support import (
    GENESIS_ACTOR,
    SWITCH_ACTOR_PREFIX,
    require_ledger_epoch,
)
from bfx_funding_bot.core.authority import AuthorityMismatch
from bfx_funding_bot.core.schema_head import ALEMBIC_DIR
from bfx_funding_bot.core.venue import Venue

from .test_ledger_schema_roles import _build, _seed

pytestmark = pytest.mark.integration

SWITCH = f"{SWITCH_ACTOR_PREFIX}switch-20261005T182054Z-fe1cc4-a1"  # prod's shape


@pytest.fixture
def db(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("ledger_s1_roles", _build))
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed(conn)  # an exchange account and one row per ledger table, no seed observation
    try:
        yield engine
    finally:
        engine.dispose()


def _epoch(engine: Engine, authority: str, actor: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            f"SELECT max(epoch_seq) + 1, '{authority}', 1, '{actor}', 'test' "
            "FROM capital_authority_epoch")


def _guard(engine: Engine, *, venue: Venue = "bitfinex") -> str | None:
    """Run the guard; the refusal text, or None when it lets the boot through."""
    url = engine.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

    async def run() -> str | None:
        async_engine = create_async_engine(url)
        try:
            async with AsyncSession(async_engine) as session:
                try:
                    await require_ledger_epoch(session, venue=venue)
                except AuthorityMismatch as exc:
                    return str(exc)
                return None
        finally:
            await async_engine.dispose()

    return asyncio.run(run())


def test_the_writers_are_the_ones_that_append_epochs() -> None:
    # The prefix the S1-7 switch wrote in prod (2026-10-05; the tool is deleted, the epoch stays).
    assert SWITCH_ACTOR_PREFIX == "ledger_seed:"
    [path] = (ALEMBIC_DIR / "versions").glob("b1c2d3e4f5a6_*.py")
    spec = importlib.util.spec_from_file_location("genesis_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert GENESIS_ACTOR == module.ACTOR


def test_the_genesis_epoch_boots(db) -> None:
    """A fresh host: whatever rests at the venue is foreign to the ledger."""
    assert _guard(db) is None


def test_a_switch_epoch_boots_an_account_the_switch_never_saw(db) -> None:
    """The switch wrote its seed with its epoch; an account added after it has no seed row and
    still boots (the per-scope seed check this guard replaced refused it)."""
    _epoch(db, "ledger", SWITCH)
    assert _guard(db) is None


@pytest.mark.parametrize("actor", ["bootstrap_simulation_db", "test", "ledger_seed"])
def test_an_epoch_written_by_anyone_else_refuses(db, actor: str) -> None:
    _epoch(db, "ledger", actor)
    assert _guard(db) == f"ledger_epoch_writer_unknown actor={actor!r}"


@pytest.mark.parametrize("venue", ["bitfinex", "simulated"])
def test_a_legacy_epoch_refuses(db, venue: Venue) -> None:
    _epoch(db, "legacy", SWITCH)
    assert _guard(db, venue=venue) == "authority_unsupported value=legacy build=ledger"


def test_the_simulated_venue_takes_any_writer(db) -> None:
    _epoch(db, "ledger", "bootstrap_simulation_db")
    assert _guard(db, venue="simulated") is None
