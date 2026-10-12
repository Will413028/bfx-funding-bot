"""49b37faec086 on real PostgreSQL: an existing 2FA enrollment gets an empty lockout budget,
and Better Auth's role can write the new columns.

Mutation checks (one at a time; revert after each):
* Drop ``server_default="0"``: the existing row's counter is NULL and
  ``test_an_existing_enrollment_starts_unlocked_with_an_empty_budget`` fails.
* Revoke the runbook's table grant before the upgrade: ``bfx_webauth`` loses UPDATE on the
  new columns and ``test_better_auth_role_can_write_the_lockout_columns`` fails.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from tests.pg_templates import alembic, template_at

pytestmark = pytest.mark.integration

_BEFORE = "2e835b6f4c12"
_REVISION = "49b37faec086"
_USER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

# docs/runbooks/fresh-host-setup.md 1c: Better Auth's role holds table-level grants on auth.
_RUNBOOK_GRANTS = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') "
    "THEN CREATE ROLE bfx_webauth; END IF; END $$",
    "GRANT USAGE ON SCHEMA auth TO bfx_webauth",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO bfx_webauth",
)


@pytest.fixture
def before(pg_templates: Any, pg_clone: Any) -> Iterator[tuple[str, Engine]]:
    """The schema before 49b37faec086 with one verified 2FA enrollment; the test upgrades."""
    url = pg_clone(pg_templates.template("auth_lockout_before", template_at(_BEFORE)))
    engine = create_engine(url)
    with engine.begin() as conn:
        for sql in _RUNBOOK_GRANTS:
            conn.exec_driver_sql(sql)
        conn.execute(text(
            'INSERT INTO auth."user" (id, name, email, "emailVerified", "createdAt", "updatedAt") '
            "VALUES (:id, 'operator', 'operator@example.invalid', true, now(), now())"
        ), {"id": _USER})
        conn.execute(text(
            'INSERT INTO auth."twoFactor" (id, secret, "backupCodes", "userId", verified) '
            "VALUES ('tf-1', 'secret', 'codes', :id, true)"
        ), {"id": _USER})
    try:
        yield url, engine
    finally:
        engine.dispose()


def test_an_existing_enrollment_starts_unlocked_with_an_empty_budget(
    before: tuple[str, Engine],
) -> None:
    url, engine = before
    alembic(url, "upgrade", _REVISION)
    with engine.connect() as conn:
        assert conn.execute(text(
            'SELECT "failedVerificationCount", "lockedUntil" FROM auth."twoFactor" '
            "WHERE id = 'tf-1'"
        )).one() == (0, None)


def test_better_auth_role_can_write_the_lockout_columns(before: tuple[str, Engine]) -> None:
    url, engine = before
    alembic(url, "upgrade", _REVISION)
    with engine.connect() as conn:
        for column in ("failedVerificationCount", "lockedUntil"):
            for privilege in ("SELECT", "UPDATE"):
                assert conn.scalar(text(
                    "SELECT has_column_privilege('bfx_webauth', 'auth.\"twoFactor\"', "
                    ":column, :privilege)"
                ), {"column": column, "privilege": privilege}), (column, privilege)
