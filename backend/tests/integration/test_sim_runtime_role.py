"""The simulation soak's runtime role ``bfx_sim``: bfx_bot inside ``bfx_sim``, nothing in ``bfx``.

``docs/runbooks/simulation-soak.md`` §2 sets it up on the host; this module applies the same
statements to two clones of a production-faithful head database (``fresh-host-setup.md`` §1a
default privileges), one standing for the live ``bfx`` and one for ``bfx_sim``:

* the live database: CONNECT revoked from PUBLIC and granted to ``bfx_bot`` (and the other
  runtime roles on the host);
* ``bfx_sim``: a LOGIN role, member of ``bfx_bot`` with ``INHERIT FALSE, SET TRUE``, CONNECT on
  the simulation database only, and ``ALTER ROLE bfx_sim IN DATABASE bfx_sim SET role =
  'bfx_bot'`` so every session there runs as ``bfx_bot`` with no change to the application.

CONNECT is checked against the login role before any role setting applies, and the
membership passes no privilege by inheritance, so the credential cannot open the live
database even with ``-c role=bfx_bot``. Migrations and ``bootstrap_simulation_db.py`` keep
running as the owner (``sim-migrate.env``), as production's do.

Mutation checks (one at a time; revert after each, run this file):

* ``GRANT CONNECT ON DATABASE <live> TO bfx_sim``: the live-database tests fail.
* Leave the live database's PUBLIC CONNECT in place (skip its REVOKE): the same tests fail.
* Membership ``WITH INHERIT TRUE``: the live-database tests fail (``bfx_bot``'s CONNECT is
  inherited) and so does the no-own-privilege test.
* Drop the ``IN DATABASE ... SET role`` line: the sim-database tests fail on ``current_user``
  and ``permission denied``.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts import bootstrap_simulation_db, sim_soak_report

from .sim_daemon import HOUR, SCOPE, T0, close_sim_env, make_sim_env
from .test_sim_venue_schema_roles import _BUILDS

pytestmark = pytest.mark.integration

ROLE = "bfx_sim"
PASSWORD = "sim-runtime-role-test"
_TEMPLATE = "sim_venue_prod_faithful"


@dataclass(frozen=True)
class Databases:
    live: str  # owner URL of the database standing for live ``bfx``
    sim: str  # owner URL of the simulation database

    def as_sim_role(self, url: str, *, driver: str = "postgresql+psycopg") -> str:
        return make_url(url).set(drivername=driver, username=ROLE, password=PASSWORD
                                 ).render_as_string(hide_password=False)


def _name(url: str) -> str:
    database = make_url(url).database
    assert database
    return database


def _host_setup(owner_url: str, live: str, sim: str) -> None:
    """simulation-soak.md §2, with the clones' names for ``bfx`` and ``bfx_sim``."""
    engine = create_engine(owner_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{ROLE}') "
                f"THEN CREATE ROLE {ROLE}; END IF; END $$")
            # Cluster-wide, so set on every use, never only when a template is built.
            conn.exec_driver_sql(f"ALTER ROLE {ROLE} LOGIN PASSWORD '{PASSWORD}'")
            conn.exec_driver_sql(f"GRANT bfx_bot TO {ROLE} WITH INHERIT FALSE, SET TRUE")
            conn.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{live}" TO bfx_bot')
            conn.exec_driver_sql(f'REVOKE CONNECT ON DATABASE "{live}" FROM PUBLIC')
            conn.exec_driver_sql(f'REVOKE CONNECT ON DATABASE "{sim}" FROM PUBLIC')
            conn.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{sim}" TO {ROLE}')
            conn.exec_driver_sql(f"ALTER ROLE {ROLE} IN DATABASE \"{sim}\" SET role = 'bfx_bot'")
    finally:
        engine.dispose()


@pytest.fixture
def dbs(pg_templates: Any, pg_clone: Any) -> Iterator[Databases]:
    template = pg_templates.template(_TEMPLATE, _BUILDS[_TEMPLATE])
    live, sim = pg_clone(template), pg_clone(template)
    _host_setup(live, _name(live), _name(sim))
    yield Databases(live=live, sim=sim)


def _connect(url: str, **options: Any) -> psycopg.Connection[Any]:
    parsed = make_url(url)
    return psycopg.connect(host=parsed.host, port=parsed.port, user=parsed.username,
                           password=parsed.password, dbname=parsed.database, autocommit=True,
                           **options)


# -- (a) the credential does not open the live database ------------------------------------


@pytest.mark.parametrize("options", [{}, {"options": "-c role=bfx_bot"}],
                         ids=["plain", "role-startup-option"])
def test_the_sim_role_cannot_connect_to_the_live_database(dbs: Databases, options: dict[str, str]) -> None:
    with pytest.raises(psycopg.OperationalError,
                       match=r'permission denied for database "[^"]+"[\s\S]*CONNECT privilege'):
        _connect(dbs.as_sim_role(dbs.live), **options).close()


def test_the_live_database_still_admits_bfx_bot_and_the_catalog_agrees(dbs: Databases) -> None:
    """The rejection is the CONNECT check on the login role: ``bfx_bot`` itself keeps CONNECT,
    the member does not inherit it, and may still become it (SET)."""
    with _connect(dbs.live) as conn:
        row = conn.execute(
            "SELECT has_database_privilege('bfx_bot', current_database(), 'CONNECT'),"
            f"       has_database_privilege('{ROLE}', current_database(), 'CONNECT'),"
            f"       pg_has_role('{ROLE}', 'bfx_bot', 'USAGE'),"
            f"       pg_has_role('{ROLE}', 'bfx_bot', 'SET')").fetchone()
    assert row == (True, False, False, True)


# -- (b) inside bfx_sim it is bfx_bot, not the owner ----------------------------------------


def test_a_sim_session_runs_as_bfx_bot(dbs: Databases) -> None:
    with _connect(dbs.as_sim_role(dbs.sim)) as conn:
        assert conn.execute("SELECT session_user, current_user").fetchone() == (ROLE, "bfx_bot")
        # A pool's reset (RESET ALL / DISCARD ALL) returns to the database-role default.
        conn.execute("RESET ALL")
        assert conn.execute("SELECT current_user").fetchone() == ("bfx_bot",)
        conn.execute("DISCARD ALL")
        assert conn.execute("SELECT current_user").fetchone() == ("bfx_bot",)


def test_the_sim_role_holds_nothing_of_its_own(dbs: Databases) -> None:
    """Every privilege comes from SET ROLE, none from inheritance: as itself (``SET ROLE
    NONE``) the login role reaches no table."""
    with _connect(dbs.sim) as conn:
        held = conn.execute(
            "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p', 'v', 'S') "
            "AND n.nspname IN ('public', 'legacy_archive') AND ("
            f" has_table_privilege('{ROLE}', c.oid, 'SELECT, INSERT, UPDATE, DELETE')"
            f" OR has_any_column_privilege('{ROLE}', c.oid, 'SELECT, INSERT, UPDATE'))"
        ).fetchall()
        tables = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").fetchone()
    assert tables is not None and tables[0] > 50
    assert held == []
    with _connect(dbs.as_sim_role(dbs.sim)) as conn:
        conn.execute("SET ROLE NONE")
        assert conn.execute("SELECT current_user").fetchone() == (ROLE,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM public.trading_state LIMIT 1")


def _column_grant(conn: psycopg.Connection[Any]) -> tuple[str, str, str]:
    """A table where bfx_bot may UPDATE some columns but not the table: (table, granted
    column, refused column), read from the catalog as the owner."""
    row = conn.execute("""
        SELECT c.relname, g.attname, r.attname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute g ON g.attrelid = c.oid AND g.attnum > 0 AND NOT g.attisdropped
             AND g.attgenerated = ''
             AND has_column_privilege('bfx_bot', c.oid, g.attnum, 'UPDATE')
        JOIN pg_attribute r ON r.attrelid = c.oid AND r.attnum > 0 AND NOT r.attisdropped
             AND r.attgenerated = ''
             AND NOT has_column_privilege('bfx_bot', c.oid, r.attnum, 'UPDATE')
        WHERE c.relkind = 'r' AND NOT has_table_privilege('bfx_bot', c.oid, 'UPDATE')
        ORDER BY c.relname, g.attnum, r.attnum LIMIT 1""").fetchone()
    assert row is not None, "no column-level UPDATE grant to bfx_bot at head"
    return str(row[0]), str(row[1]), str(row[2])


def test_column_grants_apply_to_the_sim_session_as_to_bfx_bot(dbs: Databases) -> None:
    with _connect(dbs.sim) as conn:
        table, granted, refused = _column_grant(conn)
    with _connect(dbs.as_sim_role(dbs.sim)) as conn:
        # ACLs are checked when the statement starts, rows or not.
        conn.execute(f'UPDATE "{table}" SET "{granted}" = DEFAULT WHERE false')
        with pytest.raises(psycopg.errors.InsufficientPrivilege,
                           match=f'permission denied for table {table}'):
            conn.execute(f'UPDATE "{table}" SET "{refused}" = DEFAULT WHERE false')


@pytest.mark.asyncio
async def test_an_owner_exempt_trigger_still_refuses_the_sim_session(dbs: Databases) -> None:
    """``guard_runtime_policy_head`` (7d2a9c4e6b13) lets the table owner through and holds
    everyone else to "advance by one revision". The owner prepares the scope (as the soak's
    bootstrap does, with sim-migrate.env); the sim session holds the column grant yet the
    trigger refuses it, so it is not the owner. The owner's identical statement passes."""
    import argparse

    owner = make_url(dbs.sim).set(drivername="postgresql+asyncpg"
                                  ).render_as_string(hide_password=False)
    await bootstrap_simulation_db.run(argparse.Namespace(
        exchange_account_id=SCOPE.exchange_account_id, symbol=["fUST"], disabled_symbol=["fUSD"],
        max_offer_amount=Decimal("200"), min_period_days=2, max_period_days=2,
        max_open_offers=6, rate_floor_ratio=Decimal("0.5"), min_rate_apr=Decimal("0.01"),
        activate=False), database_url=owner, allowed_realms=("ci",))
    statement = "UPDATE capital_policy_heads SET revision = revision"
    with _connect(dbs.as_sim_role(dbs.sim)) as conn:
        assert conn.execute(
            "SELECT has_column_privilege('capital_policy_heads', 'revision', 'UPDATE')"
        ).fetchone() == (True,)
        with pytest.raises(psycopg.errors.RaiseException,
                           match="runtime policy head may only advance to the next revision"):
            conn.execute(statement)
    with _connect(dbs.sim) as conn:
        assert conn.execute(statement).rowcount == 2


# -- (c) the sim runtime itself, on the new credential ---------------------------------------


@pytest.mark.asyncio
async def test_the_simulated_bot_and_the_report_run_on_the_sim_credential(
        dbs: Databases, monkeypatch: Any, httpx_mock: Any, tmp_path: Any) -> None:
    """The bot composed as the soak's container is (``DATABASE_URL`` from sim.env) boots,
    deploys one offer and reports, all through the ``bfx_sim`` login; the fixture's seed and
    the operator's ACTIVE are the owner's, as on the host."""
    runtime = dbs.as_sim_role(dbs.sim, driver="postgresql+asyncpg")
    owner = create_engine(dbs.sim)  # make_sim_env reads only its URL
    owner.dispose()
    sim, engine = await make_sim_env(owner, monkeypatch, httpx_mock, tmp_path,
                                     env={"DATABASE_URL": runtime})
    try:
        daemon = await sim.build()
        async with daemon.db_engine.connect() as conn:
            assert (await conn.execute(text("SELECT session_user, current_user"))).one() == (
                ROLE, "bfx_bot")
        await sim.boot(daemon, T0)
        await sim.assert_sound(daemon)
        await sim.activate()
        sim.clock.advance(HOUR)
        sim.quote(daemon)
        await sim.tick(daemon)
        await sim.assert_sound(daemon)
        offers = list(sim.venue(daemon).state.offers.values())
        assert len(offers) == 1 and offers[0].resting, offers
    finally:
        await close_sim_env(sim, engine)
    criteria, events = await sim_soak_report.run(
        database_url=runtime, account=SCOPE.exchange_account_id,
        window=sim_soak_report.Window(T0, T0 + 2 * HOUR), realm="ci")
    assert criteria and events > 0
