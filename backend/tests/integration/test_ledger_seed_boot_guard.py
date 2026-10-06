"""``require_ledger_seed`` (H-1) on clone PostgreSQL: the real venue boots on the latest
``ledger`` epoch by its writer -- the genesis migration needs no seed, the S1-7 switch needs
every scope's ``legacy_seed`` observation, and any other writer refuses. It reads the epoch and
the ledger only, never the frozen legacy tables.

Mutation checks (one at a time; revert after each):

* Let an unknown writer through (drop the ``SWITCH_ACTOR_PREFIX`` refusal):
  ``test_an_epoch_written_by_anyone_else_refuses`` fails.
* Skip the seed lookup after a switch epoch:
  ``test_a_switch_epoch_needs_every_scopes_seed`` fails.
* Require the seed after the genesis epoch too:
  ``test_the_genesis_epoch_needs_no_seed`` fails.
"""
from __future__ import annotations

import asyncio
import importlib.util
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bfx_funding_bot.apps import ledger_seed
from bfx_funding_bot.apps.authority_support import (
    GENESIS_ACTOR,
    SWITCH_ACTOR_PREFIX,
    require_ledger_seed,
)
from bfx_funding_bot.core.authority import AuthorityMismatch
from bfx_funding_bot.core.schema_head import ALEMBIC_DIR
from bfx_funding_bot.core.venue import Venue
from bfx_funding_bot.modules.ledger import Scope

from .test_ledger_schema_roles import _A, _build, _seed
from .test_ledger_seed_schema import _owner_seed_observation

pytestmark = pytest.mark.integration

SCOPE = Scope(UUID(_A), "ci")
OTHER = Scope(UUID("00000000-0000-0000-0000-00000000b001"), "ci")
SWITCH = f"{SWITCH_ACTOR_PREFIX}switch-20261005T182054Z-fe1cc4-a1"  # prod's shape


@pytest.fixture
def db(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("ledger_s1_roles", _build))
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed(conn)  # the scope's exchange account and one row per ledger table
        conn.exec_driver_sql(
            f"INSERT INTO exchange_accounts(id,venue,label) VALUES "
            f"('{OTHER.exchange_account_id}','bitfinex','other')")
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


def _seeded(engine: Engine) -> None:
    with engine.begin() as conn:
        _owner_seed_observation(conn)  # scope ``SCOPE``


def _guard(engine: Engine, *, venue: Venue = "bitfinex",
           scopes: tuple[Scope, ...] = (SCOPE,)) -> str | None:
    """Run the guard; the refusal text, or None when it lets the boot through."""
    url = engine.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

    async def run() -> str | None:
        async_engine = create_async_engine(url)
        try:
            async with AsyncSession(async_engine) as session:
                try:
                    await require_ledger_seed(session, venue=venue, scopes=scopes)
                except AuthorityMismatch as exc:
                    return str(exc)
                return None
        finally:
            await async_engine.dispose()

    return asyncio.run(run())


def test_the_writers_are_the_ones_that_append_epochs() -> None:
    assert SWITCH_ACTOR_PREFIX == ledger_seed.SWITCH_ACTOR_PREFIX
    [path] = (ALEMBIC_DIR / "versions").glob("b1c2d3e4f5a6_*.py")
    spec = importlib.util.spec_from_file_location("genesis_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert GENESIS_ACTOR == module.ACTOR


def test_the_genesis_epoch_needs_no_seed(db) -> None:
    """A fresh host: whatever rests at the venue is foreign to the ledger."""
    assert _guard(db) is None
    assert _guard(db, scopes=(SCOPE, OTHER)) is None


def test_a_switch_epoch_needs_every_scopes_seed(db) -> None:
    _epoch(db, "ledger", SWITCH)
    assert _guard(db) == f"ledger_seed_missing scope={_A}:ci"
    _seeded(db)
    assert _guard(db) is None
    # Another scope's seed is not this one's.
    assert _guard(db, scopes=(SCOPE, OTHER)) == (
        f"ledger_seed_missing scope={OTHER.exchange_account_id}:ci")


@pytest.mark.parametrize("actor", ["bootstrap_simulation_db", "test", "ledger_seed"])
def test_an_epoch_written_by_anyone_else_refuses(db, actor: str) -> None:
    _seeded(db)
    _epoch(db, "ledger", actor)
    assert _guard(db) == f"ledger_epoch_writer_unknown actor={actor!r}"


def test_a_legacy_epoch_refuses(db) -> None:
    _epoch(db, "legacy", "test")
    assert _guard(db) == "authority_unsupported value=legacy build=ledger"


def test_the_simulated_venue_never_needs_the_seed(db) -> None:
    _epoch(db, "ledger", SWITCH)
    assert _guard(db, venue="simulated") is None
    _epoch(db, "ledger", "bootstrap_simulation_db")
    assert _guard(db, venue="simulated") is None
