"""One drill script on two schemas (`ledger_digest.py` across 545b2877e9bd), real PostgreSQL.

545b2877e9bd drops the realm column from every row. The restore test of the release that
carries it runs the TARGET release's `ledger_digest.py` on both sides of that migration in one
run: the verification phase reads the restored copy and production while both still have the
column, then the rehearsal upgrades the copy and reads its bounds again without it. The script
names the exchange account only, so it is the same text on both.

The restore point is simulated as in tests/integration/test_ledger_restore_verification.py
(clones of one seeded template), here stopped at the revision before 545b2877e9bd.

Mutation: derive the scope from ``(exchange_account_id, deployment_environment)`` again in
``bounds_script``: ``test_the_bounds_are_the_same_before_and_after_the_migration`` fails (psql:
column does not exist).
"""
from __future__ import annotations

import psycopg
import pytest

from tests.integration.test_ledger_restore_verification import (
    _bounds,
    _digest,
    _libpq,
    _production_moves_on,
    _verify,
    ledger,
    restore_point,
)
from tests.integration.test_ledger_schema_roles import _A, _prepare
from tests.pg_templates import alembic, template_at

pytestmark = pytest.mark.integration

_REVISION = "545b2877e9bd"
_BEFORE = "96535191e443"
_build_before = template_at(_BEFORE, _prepare)


def _template(url: str) -> None:
    _build_before(url)
    restore_point(url)


def _realm_columns(url: str) -> int:
    with psycopg.connect(_libpq(url)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
            "AND column_name = 'deployment_environment' AND table_name = ANY(%s)",
            (list(ledger.LEDGER_TABLES),)).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def clusters(pg_templates, pg_clone) -> tuple[str, str]:
    """A restored copy and production, both at the revision that still has the column."""
    template = pg_templates.template("ledger_digest_schema_skew", _template)
    restored, production = pg_clone(template), pg_clone(template)
    _production_moves_on(production)
    return restored, production


def test_the_bounds_are_the_same_before_and_after_the_migration(clusters) -> None:
    restored, _ = clusters
    assert _realm_columns(restored) > 0
    before = _bounds(restored)
    alembic(restored, "upgrade", _REVISION)
    assert _bounds(restored).migration_heads == (_REVISION,)
    alembic(restored, "upgrade", "head")
    after = _bounds(restored)
    assert before.migration_heads == (_BEFORE,)
    # Head is 545b2877e9bd or a later revision: the copy went through the drop either way.
    assert after.migration_heads != before.migration_heads and _realm_columns(restored) == 0
    [scope] = after.scopes
    assert scope.account == _A
    assert after.scopes == before.scopes
    assert (after.pending, after.epoch, after.inconsistencies) == (
        before.pending, before.epoch, before.inconsistencies)


def test_one_run_verifies_with_the_column_then_rehearses_without_it(clusters) -> None:
    restored, production = clusters
    # Verification phase: the copy and production both still have the column.
    summary = _verify(restored, production)
    [scope] = summary["scopes"]
    assert scope["exchange_account_id"] == _A
    assert (scope["clock_revision"], scope["production_clock_revision"]) == (1, 2)
    assert summary["tables"]["submission_attempt_journal"]["count"] == 2
    # Rehearsal: the candidate's migration on the copy, then its bounds again.
    before = _bounds(restored)
    alembic(restored, "upgrade", "head")
    after = _bounds(restored)
    assert after.scopes == before.scopes
    # The bounded COPY script also runs on the migrated copy: same rows, one column fewer.
    migrated = _digest(restored, after, restored=True)
    assert not migrated.invalid
    assert migrated.counts == _digest(production, before, restored=False).counts
