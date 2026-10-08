"""Migrated template databases, cloned per test.

Replaying the Alembic chain costs seconds; ``CREATE DATABASE ... TEMPLATE`` copies
a migrated database in milliseconds. A test that needs a database at some
revision (with whatever pre-state its migration path requires) builds that state
once per session into a template, and every test gets its own byte-identical
copy. What a template holds is decided by its build function, so a test that
verifies an upgrade or downgrade still runs it: either the build does (and the
test asserts on the result) or the test does, starting from a clone.

Roles are cluster-wide, so they are shared between clones exactly as they were
between tests reusing one database. Default privileges and grants live in the
database and so travel with each copy.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def database_url(url: str, database: str) -> str:
    return make_url(url).set(database=database).render_as_string(hide_password=False)


@contextmanager
def database_url_env(url: str) -> Iterator[None]:
    """alembic/env.py reads DATABASE_URL through Settings(); point it at ``url``."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def alembic(url: str, name: str, *args: str) -> None:
    """Run one Alembic command (``upgrade``, ``downgrade``, ``check``) in-process."""
    from alembic.config import Config

    from alembic import command

    config = Config(str(ALEMBIC_INI))
    # env.py's fileConfig() would rewire the pytest process's logging (root level and handlers,
    # every existing logger disabled), so a later caplog test would see nothing.
    config.attributes["configure_logger"] = False
    with database_url_env(url):
        getattr(command, name)(config, *args)


class TemplateDatabases:
    """Template databases on one PostgreSQL cluster, built lazily and cached."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._built: dict[str, object] = {}

    def _admin(self, sql: str) -> None:
        engine = create_engine(database_url(self._url, "postgres"), isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql(sql)
        finally:
            engine.dispose()

    def template(
        self, name: str, build: Callable[[str], object] | None = None, *, base: str | None = None
    ) -> str:
        """Build ``name`` once by running ``build(url)`` on an empty database
        (or on a copy of the template ``base``, to continue its migration path).

        Returns the template's name; ``build``'s return value is kept and
        available through :meth:`built`.
        """
        if name not in self._built:
            if build is None:
                raise KeyError(f"template {name!r} has not been built")
            self._admin(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{base}"' if base else ""))
            try:
                self._built[name] = build(database_url(self._url, name))
            except BaseException:
                self._admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                raise
        return name

    def built(self, name: str) -> object:
        return self._built[name]

    def clone(self, template: str, database: str | None = None) -> str:
        """A fresh copy of ``template``; returns its URL."""
        database = database or f"{template[:40]}_{uuid4().hex[:12]}"
        self._admin(f'CREATE DATABASE "{database}" TEMPLATE "{template}"')
        return database_url(self._url, database)

    def recreate(self, database: str, template: str) -> None:
        """Replace ``database`` (keeping its name, so its URL) with a copy of ``template``."""
        self._admin(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        self._admin(f'CREATE DATABASE "{database}" TEMPLATE "{template}"')

    def drop(self, url: str) -> None:
        self._admin(f'DROP DATABASE IF EXISTS "{make_url(url).database}" WITH (FORCE)')


# A test that must plant rows of a realm other than the stamp's (to prove a reader filters by
# realm) disables the realm trigger on purpose, in a visible statement, on its own clone.
DISABLE_REALM_TRIGGERS_SQL = """DO $$ DECLARE r regclass; BEGIN
  FOR r IN SELECT tgrelid::regclass FROM pg_trigger WHERE tgname = 'database_realm_write' LOOP
    EXECUTE 'ALTER TABLE ' || r::text || ' DISABLE TRIGGER database_realm_write';
  END LOOP; END $$"""


def disable_realm_triggers(engine: Engine) -> None:
    """Switch the realm trigger off on every table of this (per-test clone) database."""
    with engine.begin() as conn:
        conn.exec_driver_sql(DISABLE_REALM_TRIGGERS_SQL)


# The archived legacy tables refuse every write, the owner's too (c2d3e4f5a6b7,
# ``archive_frozen``). A test that plants pre-switch history in a head database opens them on
# its own clone, in this visible statement.
OPEN_LEGACY_ARCHIVE_SQL = """DO $$ DECLARE r regclass; BEGIN
  FOR r IN SELECT tgrelid::regclass FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
           WHERE t.tgname = 'archive_frozen' AND c.relnamespace = 'legacy_archive'::regnamespace
             AND c.relname <> 'manifest' LOOP
    EXECUTE 'ALTER TABLE ' || r::text || ' DISABLE TRIGGER archive_frozen';
  END LOOP; END $$"""


def stamp_realm(url: str, realm: str = "ci") -> None:
    """The owner's one-time ``database_realm`` stamp on a migrated, still-empty database.

    Migrating an empty database leaves it unstamped, and an unstamped database refuses
    every realm write; a fresh host stamps it once (docs/runbooks/fresh-host-setup.md).
    """
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO database_realm (realm, stamped_at_ms, actor) "
                "VALUES (%s, (extract(epoch FROM clock_timestamp()) * 1000)::bigint, 'test')",
                (realm,),
            )
    finally:
        engine.dispose()


def upgrade_head(url: str) -> None:
    """The plain build: an empty database migrated to head and stamped ``ci``."""
    alembic(url, "upgrade", "head")
    stamp_realm(url, "ci")


# The last migration written before docs/adr/2026-10-08-forward-only-migrations.md: downgrades
# from it down the chain still run, a downgrade from any later migration raises.
LAST_REVERSIBLE_REVISION = "8ac3b44460fc"


def template_at(
    revision: str, prepare: Callable[[str], object] | None = None
) -> Callable[[str], None]:
    """The plain build stopped at ``revision``: ``prepare(url)`` on the empty database (roles,
    default privileges), the upgrade to ``revision``, and the ``ci`` stamp.

    A test that downgrades starts from :data:`LAST_REVERSIBLE_REVISION` this way, not from
    head: ``pg_templates.template(name, template_at(LAST_REVERSIBLE_REVISION))``.
    """

    def build(url: str) -> None:
        if prepare is not None:
            prepare(url)
        alembic(url, "upgrade", revision)
        stamp_realm(url, "ci")

    return build
