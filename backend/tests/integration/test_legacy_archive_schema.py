"""``c2d3e4f5a6b7``: the twelve frozen legacy tables live in ``legacy_archive``, only the owner
writes them, and only the remaining readers read them.

On the production-shaped clone of ``test_ledger_schema_roles`` (default privileges hand every
new table and sequence to ``bfx_bot`` and ``bfx_webapi``; ``bfx_webauth`` and
``bfx_cutover_reader`` exist).

Mutation checks (one at a time; revert after each):

* Skip ``_revoke_all`` in ``upgrade``: ``test_no_role_but_the_owner_can_write_the_archive``
  fails (the default write grants moved with the tables).
* Point ``archived_execution_history.EVENT_LOG`` at ``public``:
  ``test_the_web_api_reads_the_archived_history`` fails (no such relation).
* Grant ``account_id`` in ``WEBAPI_ARCHIVE_PRIVILEGES``: ``test_each_reader_holds_exactly_its_reads``
  fails.
* Skip the manifest's privilege record (``revoked_privileges = '[]'``): the downgrade test fails
  (the pre-archive grants are not given back).
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.archived_execution_history import ArchivedExecutionHistory
from bfx_funding_bot.modules.ledger import Scope
from tests.pg_templates import alembic, stamp_realm

from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture
from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic/versions"
_PRE_ARCHIVE = "b1c2d3e4f5a6"
_ACCOUNT = "00000000-0000-0000-0000-0000000ac001"
_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader", "public")
# Written out, not imported from the migration: shrinking its list must fail here.
TABLES = (
    "event_log", "event_prefix_hashes", "projection_heads", "position_state",
    "venue_offer_state", "venue_credit_state", "reconcile_observation", "offer_claims",
    "submission_attempts", "execution_uncertainties", "capital_snapshot_queries",
    "capital_snapshots",
)
WEBAPI_EVENT_COLUMNS = {
    "event_seq", "exchange_account_id", "deployment_environment", "event_type",
    "occurred_at_ms", "venue_offer_id", "cid", "payload",
}


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "legacy_archive_schema", _VERSIONS / "c2d3e4f5a6b7_legacy_archive_schema.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _sequences(conn: Any, schema: str) -> list[str]:
    return list(conn.execute(text(
        "SELECT s.relname FROM pg_class s JOIN pg_depend d ON d.objid = s.oid "
        "AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i') "
        "JOIN pg_class t ON t.oid = d.refobjid WHERE s.relkind = 'S' "
        "AND t.relnamespace = CAST(:s AS regnamespace) AND t.relname = ANY(:t)"),
        {"s": schema, "t": list(TABLES)}).scalars())


def _acl(engine: Engine, schema: str) -> set[tuple[str, ...]]:
    """Every (object, column, grantee, privilege, grantable) a non-owner holds on the twelve,
    their columns and their owned sequences in ``schema``."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT c.relname, NULL, CASE a.grantee WHEN 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(a.grantee) END, a.privilege_type, a.is_grantable
            FROM pg_class c, aclexplode(c.relacl) a
            WHERE c.relnamespace = CAST(:s AS regnamespace) AND a.grantee <> c.relowner
              AND (c.relname = ANY(:t) OR c.relname = ANY(:q))
            UNION ALL
            SELECT c.relname, att.attname, CASE a.grantee WHEN 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(a.grantee) END, a.privilege_type, a.is_grantable
            FROM pg_class c JOIN pg_attribute att ON att.attrelid = c.oid AND att.attnum > 0,
                 aclexplode(att.attacl) a
            WHERE c.relnamespace = CAST(:s AS regnamespace) AND a.grantee <> c.relowner
              AND c.relname = ANY(:t)"""),
            {"s": schema, "t": list(TABLES), "q": _sequences(conn, schema)}).all()
    return {tuple(row) for row in rows}


def _seed(engine: Engine, schema: str) -> None:
    """The owner's legacy history (the freeze passes the owner): an account, two events."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"INSERT INTO exchange_accounts (id, venue, label) VALUES ('{_ACCOUNT}', 'bitfinex', "
            "'archive') ON CONFLICT DO NOTHING")
        for seq, (event_type, payload) in enumerate((
            ("RESERVATION_INTENT", '{"symbol": "fUST", "size_usdt": "150", "rate": 0.0002}'),
            ("ORDER_FILL", '{"symbol": "fUST", "amount": "150", "fill_rate": 0.0002}'),
        ), start=1):
            conn.exec_driver_sql(
                f"INSERT INTO {schema}.event_log (account_id, exchange_account_id, "
                "deployment_environment, event_type, cid, venue_offer_id, payload, "
                f"occurred_at_ms) VALUES ('{_ACCOUNT}', '{_ACCOUNT}', 'ci', '{event_type}', "
                f"{seq}, 'offer-{seq}', '{payload}', {1000 + seq})")


# -- the move -------------------------------------------------------------------------------

def test_the_twelve_live_in_the_archive_and_nowhere_else(ledger_db) -> None:  # noqa: F811
    with ledger_db.connect() as conn:
        for table in TABLES:
            assert conn.scalar(text("SELECT to_regclass(:n)"), {"n": f"public.{table}"}) is None
            assert conn.scalar(text("SELECT to_regclass(:n)"),
                               {"n": f"legacy_archive.{table}"}) is not None, table
        frozen = set(conn.execute(text(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = 'legacy_authority_write' "
            "AND c.relnamespace = 'legacy_archive'::regnamespace")).scalars())
        manifest = {row.table_name: row for row in conn.execute(text(
            "SELECT table_name, row_count, content_sha256, archived_by_revision "
            "FROM legacy_archive.manifest"))}
        assert set(conn.execute(text(
            "SELECT relname FROM pg_class WHERE relnamespace = 'legacy_archive'::regnamespace "
            "AND relkind = 'r'")).scalars()) == {*TABLES, "manifest"}
    assert frozen == set(TABLES)  # the freeze moved with them
    assert set(manifest) == set(TABLES)
    assert {row.archived_by_revision for row in manifest.values()} == {"c2d3e4f5a6b7"}
    assert set(_migration().TABLES) == set(TABLES)


def test_no_live_table_references_the_archive(ledger_db) -> None:  # noqa: F811
    """The request outbox's foreign key into the event log is gone (its column stays); only the
    equally frozen release archive still points in."""
    with ledger_db.connect() as conn:
        inbound = set(conn.execute(text(
            "SELECT n.nspname || '.' || src.relname || ':' || con.conname FROM pg_constraint con "
            "JOIN pg_class src ON src.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = src.relnamespace "
            "JOIN pg_class dst ON dst.oid = con.confrelid "
            "WHERE con.contype = 'f' AND dst.relnamespace = 'legacy_archive'::regnamespace "
            "AND src.relnamespace <> 'legacy_archive'::regnamespace")).scalars())
        column = conn.scalar(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'uncertainty_resolution_requests' "
            "AND column_name = 'resolved_event_seq'"))
    assert inbound == {"release_archive.canary_command_permits:fk_canary_permits_attempt"}
    assert column == 1


# -- writes ---------------------------------------------------------------------------------

def test_no_role_but_the_owner_can_write_the_archive(ledger_db) -> None:  # noqa: F811
    with ledger_db.connect() as conn:
        sequences = _sequences(conn, "legacy_archive")
        assert sequences, "the identity sequences moved with their tables"
        for role in _ROLES:
            for table in (*TABLES, "manifest"):
                name = f"legacy_archive.{table}"
                for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
                                  "TRIGGER"):
                    assert not conn.scalar(text("SELECT has_table_privilege(:r, :t, :p)"),
                                           {"r": role, "t": name, "p": privilege}), (
                        role, table, privilege)
                assert not conn.scalar(text(
                    "SELECT has_any_column_privilege(:r, :t, 'INSERT,UPDATE,REFERENCES')"),
                    {"r": role, "t": name}), (role, table)
            for sequence in sequences:
                assert not conn.scalar(text(
                    "SELECT has_sequence_privilege(:r, :s, 'USAGE,UPDATE')"),
                    {"r": role, "s": f"legacy_archive.{sequence}"}), (role, sequence)
            assert not conn.scalar(text(
                "SELECT has_schema_privilege(:r, 'legacy_archive', 'CREATE')"), {"r": role})
    with pytest.raises(Exception, match="permission denied"), ledger_db.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("DELETE FROM legacy_archive.event_log WHERE false")


def test_the_freeze_refuses_a_write_grant_given_back(ledger_db) -> None:  # noqa: F811
    """Second layer: a hand GRANT does not reopen the archive while the epoch is ledger."""
    with pytest.raises(Exception, match="legacy write after the authority switch: event_log"), \
            ledger_db.begin() as conn:
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA legacy_archive TO bfx_bot")
        conn.exec_driver_sql("GRANT DELETE ON legacy_archive.event_log TO bfx_bot")
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("DELETE FROM legacy_archive.event_log WHERE false")


# -- reads ----------------------------------------------------------------------------------

def _column_reads(conn: Any, role: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for table, column in conn.execute(text(
            "SELECT c.relname, a.attname FROM pg_class c JOIN pg_attribute a "
            "ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "WHERE c.relnamespace = 'legacy_archive'::regnamespace AND c.relkind = 'r' "
            "AND has_column_privilege(:r, c.oid, a.attnum, 'SELECT')"), {"r": role}):
        found.setdefault(table, set()).add(column)
    return found


def test_each_reader_holds_exactly_its_reads(ledger_db) -> None:  # noqa: F811
    with ledger_db.connect() as conn:
        usage = {role: conn.scalar(text(
            "SELECT has_schema_privilege(:r, 'legacy_archive', 'USAGE')"), {"r": role})
            for role in _ROLES}
        webapi = _column_reads(conn, "bfx_webapi")
        reader = _column_reads(conn, "bfx_cutover_reader")
        table_reads = {role: [t for t in (*TABLES, "manifest") if conn.scalar(text(
            "SELECT has_table_privilege(:r, :t, 'SELECT')"),
            {"r": role, "t": f"legacy_archive.{t}"})] for role in _ROLES}
        nobody = {role: _column_reads(conn, role) for role in ("bfx_bot", "bfx_webauth", "public")}
    assert usage == {"bfx_bot": False, "bfx_webapi": True, "bfx_webauth": False,
                     "bfx_cutover_reader": True, "public": False}
    assert webapi == {"event_log": WEBAPI_EVENT_COLUMNS}
    assert table_reads == {role: [] for role in _ROLES}
    assert nobody == {role: {} for role in ("bfx_bot", "bfx_webauth", "public")}
    # The switch scaffolding's reader keeps the column reads it had (PR-D takes them back).
    assert set(reader) <= set(TABLES) and "manifest" not in reader
    assert reader["event_log"] >= WEBAPI_EVENT_COLUMNS


def test_the_web_api_reads_the_archived_history(ledger_db) -> None:  # noqa: F811
    _seed(ledger_db, "legacy_archive")

    async def read() -> Any:
        engine = create_async_engine(_url(ledger_db).replace("+psycopg", "+asyncpg"))
        try:
            async with async_sessionmaker(engine)() as session, session.begin():
                await session.execute(text("SET LOCAL ROLE bfx_webapi"))
                return await ArchivedExecutionHistory().list_executions(
                    session, Scope(UUID(_ACCOUNT), "ci"), before=None, limit=1, event_type=None)
        finally:
            await engine.dispose()

    page = asyncio.run(read())
    [event] = page.events
    assert (event.event_type, event.amount, event.rate, event.symbol, event.cid) == (
        "ORDER_FILL", "150", 0.0002, "fUST", 2)
    assert page.next_before == event.event_key


# -- fresh build, downgrade -----------------------------------------------------------------

def _build_pre_archive(url: str) -> None:
    """The same production-shaped roles, migrated from empty straight to the revision below."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') "
            "THEN CREATE ROLE bfx_webauth; END IF; END $$")
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_webauth")
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_webauth")
    engine.dispose()
    alembic(url, "upgrade", _PRE_ARCHIVE)
    stamp_realm(url, "ci")


def test_downgrade_restores_public_and_every_grant_exactly(
    ledger_db, pg_templates, pg_clone,  # noqa: F811
) -> None:
    never_archived = create_engine(pg_clone(pg_templates.template(
        "legacy_archive_pre_archive", _build_pre_archive)))
    try:
        expected = _acl(never_archived, "public")
    finally:
        never_archived.dispose()
    assert {("event_log", None, "bfx_bot", "INSERT", False),
            ("event_log", None, "bfx_webapi", "SELECT", False)} <= expected
    url = _url(ledger_db)
    ledger_db.dispose()
    alembic(url, "downgrade", _PRE_ARCHIVE)
    assert _acl(ledger_db, "public") == expected
    with ledger_db.connect() as conn:
        assert conn.scalar(text("SELECT to_regnamespace('legacy_archive')")) is None
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_constraint WHERE conname = "
            "'fk_uncertainty_resolution_requests_event'")) == 1
    ledger_db.dispose()
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    alembic(url, "downgrade", _PRE_ARCHIVE)  # a second round trip is as exact
    assert _acl(ledger_db, "public") == expected


def test_the_rows_move_unchanged_and_the_manifest_proves_it(ledger_db) -> None:  # noqa: F811
    url = _url(ledger_db)
    ledger_db.dispose()
    alembic(url, "downgrade", _PRE_ARCHIVE)
    _seed(ledger_db, "public")
    with ledger_db.connect() as conn:
        before = conn.execute(text(
            "SELECT to_jsonb(t)::text FROM public.event_log t ORDER BY event_seq")).scalars().all()
    ledger_db.dispose()
    alembic(url, "upgrade", "head")
    digest = _migration()._digest
    with ledger_db.connect() as conn:
        after = conn.execute(text(
            "SELECT to_jsonb(t)::text FROM legacy_archive.event_log t ORDER BY event_seq"
        )).scalars().all()
        recorded = {row.table_name: (row.row_count, row.content_sha256) for row in conn.execute(
            text("SELECT table_name, row_count, content_sha256 FROM legacy_archive.manifest"))}
        recomputed = {table: tuple(conn.execute(text(digest("legacy_archive", table))).one())
                      for table in TABLES}
        with pytest.raises(Exception, match="manifest is frozen"), conn.begin_nested():
            conn.exec_driver_sql("DELETE FROM legacy_archive.manifest")
    assert len(before) == 2 and after == before
    assert recorded == recomputed
    assert recorded["event_log"][0] == 2
