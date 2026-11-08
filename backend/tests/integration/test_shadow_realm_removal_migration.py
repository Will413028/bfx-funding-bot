"""96535191e443: the ``shadow`` realm is removed; a database is stamped ``prod`` or ``ci``.

On a disposable PostgreSQL, from the previous revision: a database stamped ``shadow``, or one
holding a ``shadow`` row, refuses the upgrade and stays where it was; otherwise both realm
CHECKs narrow, and the downgrade widens them again.

Mutations: drop the stamp precheck or the row precheck (the refusal tests fail); scan only
``current_schema()`` (the guarded other-schema refusal test fails); drop the trigger filter
(the unguarded-table test fails); keep
``'shadow'`` in either new CHECK (the tightening test fails).
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.pg_templates import DISABLE_REALM_TRIGGERS_SQL, alembic, stamp_realm

pytestmark = pytest.mark.integration

_REVISION = "96535191e443"
_BEFORE = "a9d2c4e6f813"


def _build_before(url: str) -> None:
    alembic(url, "upgrade", _BEFORE)


@pytest.fixture
def before_url(pg_templates: Any, pg_clone: Any) -> str:
    """An empty, unstamped database at the previous revision."""
    return pg_clone(pg_templates.template("norealm_before", _build_before))


def _version(url: str) -> str:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return str(conn.scalar(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()


def _checks(url: str) -> dict[str, str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return dict(conn.execute(text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname IN ('ck_database_realm_realm', 'ck_sim_venue_event_realm')"
            )).tuples().all())
    finally:
        engine.dispose()


def _run(url: str, *statements: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)
    finally:
        engine.dispose()


def _sim_event(realm: str) -> str:
    return (
        "INSERT INTO sim_venue_event(exchange_account_id, deployment_environment, seq, event_type, "
        f"schema_version, payload) VALUES ('sim-1', '{realm}', 1, 'wallet_funded', 1, '{{}}')"
    )


def _refuses(url: str, match: str) -> None:
    with pytest.raises(RuntimeError, match=match):
        alembic(url, "upgrade", _REVISION)
    assert _version(url) == _BEFORE
    checks = _checks(url)
    assert "'shadow'" in checks["ck_database_realm_realm"]
    assert "'shadow'" in checks["ck_sim_venue_event_realm"]


def test_a_database_stamped_shadow_refuses_the_upgrade(before_url: str) -> None:
    stamp_realm(before_url, "shadow")
    _refuses(before_url, "stamped realm 'shadow'")


def test_a_shadow_row_refuses_the_upgrade(before_url: str) -> None:
    stamp_realm(before_url, "ci")
    # A ci database whose trigger was switched off, in this visible statement, to plant it.
    _run(before_url, DISABLE_REALM_TRIGGERS_SQL, _sim_event("shadow"))
    _refuses(before_url, r"rows of realm 'shadow' in public\.sim_venue_event")


def test_a_shadow_row_in_another_schema_refuses_the_upgrade(before_url: str) -> None:
    # A realm-guarded table outside the search path (as projection_audit.runs is) is scanned:
    # the row is planted first, then the realm trigger is attached.
    stamp_realm(before_url, "ci")
    _run(
        before_url,
        "CREATE SCHEMA other_realm_archive",
        "CREATE TABLE other_realm_archive.runs (deployment_environment text NOT NULL)",
        "INSERT INTO other_realm_archive.runs VALUES ('shadow')",
        "CREATE TRIGGER database_realm_write BEFORE INSERT OR UPDATE OF deployment_environment "
        "ON other_realm_archive.runs FOR EACH ROW EXECUTE FUNCTION public.guard_database_realm()",
    )
    _refuses(before_url, r"rows of realm 'shadow' in other_realm_archive\.runs")


def test_a_shadow_row_in_an_unguarded_table_does_not_block(before_url: str) -> None:
    # release_archive's frozen copies carry no realm trigger; they are not live realm data.
    stamp_realm(before_url, "ci")
    _run(
        before_url,
        "CREATE SCHEMA IF NOT EXISTS release_archive",
        "CREATE TABLE release_archive.frozen_runs (deployment_environment text NOT NULL)",
        "INSERT INTO release_archive.frozen_runs VALUES ('shadow')",
    )
    alembic(before_url, "upgrade", _REVISION)
    assert _version(before_url) == _REVISION


def test_the_upgrade_narrows_both_checks_and_the_downgrade_widens_them(before_url: str) -> None:
    stamp_realm(before_url, "ci")
    _run(before_url, _sim_event("ci"))
    alembic(before_url, "upgrade", _REVISION)
    checks = _checks(before_url)
    assert "shadow" not in checks["ck_database_realm_realm"]
    assert "'prod'" in checks["ck_database_realm_realm"] and "'ci'" in checks["ck_database_realm_realm"]
    assert checks["ck_sim_venue_event_realm"] == "CHECK ((deployment_environment = 'ci'::text))"

    alembic(before_url, "downgrade", _BEFORE)
    widened = _checks(before_url)
    assert "'shadow'" in widened["ck_database_realm_realm"]
    assert "'shadow'" in widened["ck_sim_venue_event_realm"]
