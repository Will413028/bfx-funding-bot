"""``d3e4f5a6b7c8``: the cutover reader gives back what it read, then the group goes.

On the production-shaped build of ``test_ledger_schema_roles`` at ``c2d3e4f5a6b7`` (the head
before this revision), with the operator's LOGIN member ``bfx_cutover_attest`` as in prod:

* the literal ``READER_PUBLIC_COLUMNS`` is exactly what the migrations gave the group in
  ``public``, and the archive part is what ``c2d3e4f5a6b7`` granted back from its manifest;
* upgrade leaves the group nothing in the database; downgrade gives back exactly the set it
  held (every table, column and schema privilege, compared row for row); upgrade again works;
* a privilege an environment added (not a migration's) is revoked too and not given back;
* a dependency the revision does not handle (here an EXECUTE grant on a function) refuses the
  upgrade: nothing revoked, still at ``c2d3e4f5a6b7``.

Roles are cluster-wide and the shared test cluster holds many databases that still grant the
group something, so there it is never dropped. The drop itself runs on a cluster of its own
(``retire_pg_proc``): the group is gone after the upgrade, the LOGIN stays without a
membership, and the downgrade recreates the group with ``b1e2d3a4c5f6``'s marker.

Mutation checks (one at a time; revert after each):

* drop one column from ``READER_PUBLIC_COLUMNS``: the upgrade refuses
  (``test_upgrade_takes_everything_back_and_downgrade_gives_it_back`` fails);
* skip ``DROP ROLE``: ``test_on_its_own_cluster_the_group_is_dropped`` fails;
* skip the archive grants in the downgrade: the first test fails (the archive rows differ);
* drop the ``_OTHER`` refusal: ``test_a_dependency_it_does_not_handle_refuses`` fails;
* revoke only the column privileges: ``test_an_environment_grant_is_revoked_and_not_given_back``
  fails.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pytest_postgresql.factories import postgresql_proc
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests import pg_local
from tests.pg_templates import alembic, stamp_realm

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic/versions"
_BEFORE = "c2d3e4f5a6b7"
_RETIRE = "d3e4f5a6b7c8"
READER = "bfx_cutover_reader"
LOGIN = "bfx_cutover_attest"

_HELD = f"""
WITH reader AS (SELECT oid FROM pg_roles WHERE rolname = '{READER}')
SELECT 'table', n.nspname, c.relname, NULL, a.privilege_type, a.is_grantable
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a, reader
WHERE a.grantee = reader.oid
UNION ALL
SELECT 'column', n.nspname, c.relname, att.attname, a.privilege_type, a.is_grantable
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute att ON att.attrelid = c.oid AND att.attnum > 0 AND NOT att.attisdropped,
     aclexplode(att.attacl) a, reader
WHERE a.grantee = reader.oid
UNION ALL
SELECT 'schema', n.nspname, NULL, NULL, a.privilege_type, a.is_grantable
FROM pg_namespace n, aclexplode(n.nspacl) a, reader WHERE a.grantee = reader.oid
UNION ALL
SELECT 'other', d.classid::regclass::text, d.objid::text, d.objsubid::text, d.deptype::text, false
FROM pg_shdepend d, reader
WHERE d.refobjid = reader.oid AND d.refclassid = 'pg_authid'::regclass
  AND d.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND (d.deptype <> 'a' OR d.classid NOT IN ('pg_class'::regclass, 'pg_namespace'::regclass))
"""


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "retire_cutover_reader", _VERSIONS / f"{_RETIRE}_retire_cutover_reader.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _build_before(url: str) -> None:
    """``test_ledger_schema_roles._build``, stopped at the revision below, plus prod's LOGIN."""
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
    alembic(url, "upgrade", _BEFORE)
    stamp_realm(url, "ci")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{LOGIN}') "
            f"THEN CREATE ROLE {LOGIN} LOGIN NOINHERIT; END IF; END $$")
        conn.exec_driver_sql(f"GRANT {READER} TO {LOGIN}")
    engine.dispose()


def _reader_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": READER}))


def _held(engine: Engine) -> set[tuple[Any, ...]]:
    with engine.connect() as conn:
        return {tuple(row) for row in conn.execute(text(_HELD)).all()}


def _members(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text(
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            "JOIN pg_roles l ON l.oid = m.member WHERE l.rolname = :l"), {"l": LOGIN}).scalars())


@pytest.fixture
def before_db(pg_templates, pg_clone) -> Iterator[Engine]:
    engine = create_engine(pg_clone(pg_templates.template(f"cutover_reader_{_BEFORE}",
                                                          _build_before)))
    try:
        yield engine
    finally:
        engine.dispose()


def test_the_literal_is_what_the_migrations_gave(before_db) -> None:
    held = _held(before_db)
    public = {(table, column) for kind, schema, table, column, privilege, grantable in held
              if kind == "column" and schema == "public"}
    literal = {(table, column) for table, columns in _migration().READER_PUBLIC_COLUMNS.items()
               for column in columns}
    assert public == literal
    with before_db.connect() as conn:
        archived = {tuple(row) for row in conn.execute(text(_migration()._ARCHIVE_GRANTS)).all()}
    assert {(table, column) for kind, schema, table, column, *_ in held
            if schema == "legacy_archive" and kind == "column"} == archived
    assert {row[:2] for row in held if row[0] in {"schema", "table", "other"}} == {
        ("schema", "public"), ("schema", "legacy_archive")}
    assert {(row[4], row[5]) for row in held} == {("SELECT", False), ("USAGE", False)}
    assert len(public) == 314 and len(archived) == 93


def test_upgrade_takes_everything_back_and_downgrade_gives_it_back(before_db) -> None:
    url = _url(before_db)
    before = _held(before_db)
    assert before and _members(before_db) == {READER}

    alembic(url, "upgrade", _RETIRE)
    if _reader_exists(before_db):  # another database of the shared cluster still grants it
        assert _held(before_db) == set()
        with before_db.connect() as conn:
            assert conn.scalar(text(
                "SELECT has_any_column_privilege(:r, 'public.ledger_observation', 'SELECT')"),
                {"r": READER}) is False

    alembic(url, "downgrade", _BEFORE)
    assert _held(before_db) == before

    alembic(url, "upgrade", _RETIRE)
    if _reader_exists(before_db):
        assert _held(before_db) == set()


def test_an_environment_grant_is_revoked_and_not_given_back(before_db) -> None:
    before = _held(before_db)
    with before_db.begin() as conn:
        conn.exec_driver_sql(f"GRANT SELECT, INSERT ON public.trading_state TO {READER}")
    alembic(_url(before_db), "upgrade", _RETIRE)
    if _reader_exists(before_db):
        assert _held(before_db) == set()
    alembic(_url(before_db), "downgrade", _BEFORE)
    assert _held(before_db) == before


def test_a_dependency_it_does_not_handle_refuses(before_db) -> None:
    with before_db.begin() as conn:
        conn.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION public.guard_ledger_scope() TO {READER}")
    before = _held(before_db)
    with pytest.raises(RuntimeError, match="has 1 dependencies besides"):
        alembic(_url(before_db), "upgrade", _RETIRE)
    assert _held(before_db) == before
    with before_db.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _BEFORE


# -- the drop, on a cluster of its own -------------------------------------------------------

retire_pg_proc = postgresql_proc(
    executable=str((pg_local.find_pg_bin() or Path("/nonexistent-postgresql-bin")) / "pg_ctl"),
    host="127.0.0.1", port=None, user="test", password="test", dbname="test",
    postgres_options=pg_local.SERVER_OPTIONS,
)


@pytest.fixture
def own_cluster_db(retire_pg_proc) -> Iterator[Engine]:
    """One application database on a cluster nothing else uses, as in production."""
    with psycopg.connect(host=retire_pg_proc.host, port=retire_pg_proc.port,
                         user=retire_pg_proc.user, password=retire_pg_proc.password,
                         dbname="postgres", autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS retire")
        for role in (LOGIN, READER, "bfx_webauth", "bfx_bot", "bfx_webapi"):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
        conn.execute("CREATE DATABASE retire")
    url = (f"postgresql+psycopg://{retire_pg_proc.user}:{retire_pg_proc.password}"
           f"@{retire_pg_proc.host}:{retire_pg_proc.port}/retire")
    _build_before(url)
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_on_its_own_cluster_the_group_is_dropped(own_cluster_db) -> None:
    url = _url(own_cluster_db)
    before = _held(own_cluster_db)
    with own_cluster_db.connect() as conn:
        marker = conn.scalar(text(
            "SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = :r"),
            {"r": READER})
    assert marker == "created by b1e2d3a4c5f6 in retire"

    alembic(url, "upgrade", _RETIRE)
    assert not _reader_exists(own_cluster_db)
    # The operator's LOGIN stays (its password is on the host), with no membership left.
    with own_cluster_db.connect() as conn:
        assert conn.scalar(text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :l"),
                           {"l": LOGIN}) is True
    assert _members(own_cluster_db) == set()

    alembic(url, "downgrade", _BEFORE)
    assert _held(own_cluster_db) == before
    with own_cluster_db.connect() as conn:
        role = conn.execute(text(
            "SELECT rolcanlogin, shobj_description(oid, 'pg_authid') FROM pg_roles "
            "WHERE rolname = :r"), {"r": READER}).one()
    assert tuple(role) == (False, marker)
    assert _members(own_cluster_db) == set()  # an operator grant, not the migration's

    alembic(url, "upgrade", _RETIRE)
    assert not _reader_exists(own_cluster_db)
