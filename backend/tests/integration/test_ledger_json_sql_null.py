"""``a6c7e8f9b0d1``: stored JSON ``null`` becomes SQL NULL, and the literal is refused after.

The database is seeded at head, taken back to the revision before, and given the JSON literals
the old writer stored (the owner, with the row triggers off, as only a test may).

Mutation checks (one at a time; revert after each):

* Skip the UPDATE. The literals survive and the validated CHECK refuses the upgrade.
* Skip ``ENABLE TRIGGER USER``. ``test_the_upgrade_rewrites_every_literal`` finds a trigger off.
* Rewrite NOT NULL columns too. ``test_the_upgrade_refuses_a_literal_it_cannot_rewrite`` fails
  on the NOT NULL instead of the refusal.
* Skip the disabled-trigger refusal. ``test_the_upgrade_refuses_a_table_with_a_disabled_trigger``
  succeeds the upgrade instead.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.pg_templates import alembic

from .test_ledger_schema_roles import ledger_db, seeded  # noqa: F401 - fixture re-exports

pytestmark = pytest.mark.integration

_REVISION = "a6c7e8f9b0d1"
_PREVIOUS = "f5a6b7c8d9e0"
_PATH = Path(__file__).resolve().parents[2] / f"alembic/versions/{_REVISION}_ledger_json_sql_null.py"


def _migration():
    spec = importlib.util.spec_from_file_location("json_null_migration", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COLUMNS = _migration().COLUMNS


def _literals(conn) -> dict[str, int]:
    return {
        f"{table}.{column}": conn.scalar(text(
            f"SELECT count(*) FROM public.{table} WHERE jsonb_typeof({column}::jsonb) = 'null'"))
        for table, column, _ in COLUMNS
    }


def _store_literals(engine) -> set[str]:
    """Every seeded row of every nullable column gets the JSON literal; returns the columns that
    had rows."""
    stored = set()
    with engine.begin() as conn:
        for table, column, nullable in COLUMNS:
            if not nullable:
                continue
            conn.exec_driver_sql(f"ALTER TABLE public.{table} DISABLE TRIGGER USER")
            if conn.exec_driver_sql(
                f"UPDATE public.{table} SET {column} = 'null'::jsonb").rowcount:
                stored.add(f"{table}.{column}")
            conn.exec_driver_sql(f"ALTER TABLE public.{table} ENABLE TRIGGER USER")
    return stored


@pytest.fixture
def before(seeded):  # noqa: F811
    url = seeded.url.render_as_string(hide_password=False)
    seeded.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    return url, seeded


def test_the_upgrade_rewrites_every_literal(before) -> None:
    url, engine = before
    stored = _store_literals(engine)
    # The seed covers append-only tables (the case the trigger toggle exists for).
    assert {"ledger_observation_credit.flags", "capital_authority_epoch.evidence"} <= stored
    engine.dispose()
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    with engine.connect() as conn:
        assert set(_literals(conn).values()) == {0}
        disabled = conn.scalar(text(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled = 'D'"))
        assert disabled == 0
        names = set(conn.scalars(text(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' AND convalidated "
            "AND conname LIKE 'ck\\_%\\_json'")))
    assert names == {f"ck_{table}_{column}_json" for table, column, _ in COLUMNS}
    # The owner itself can no longer store the literal.
    for table, column, _ in COLUMNS:
        if f"{table}.{column}" not in stored:
            continue
        with engine.begin() as conn, pytest.raises(Exception, match=f"ck_{table}_{column}_json"):
            conn.exec_driver_sql(f"ALTER TABLE public.{table} DISABLE TRIGGER USER")
            conn.exec_driver_sql(f"UPDATE public.{table} SET {column} = 'null'::jsonb")
    # The downgrade drops only the CHECKs.
    engine.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    with engine.connect() as conn:
        assert not conn.scalar(text(
            "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'ck\\_%\\_json'"))
        assert set(_literals(conn).values()) == {0}


def test_the_upgrade_refuses_a_table_with_a_disabled_trigger(before) -> None:
    url, engine = before
    _store_literals(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE public.ledger_observation_credit DISABLE TRIGGER immutable_ledger_write")
    engine.dispose()
    with pytest.raises(Exception, match=r"refuse to rewrite public\.ledger_observation_credit"):
        alembic(url, "upgrade", "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _PREVIOUS


def test_the_upgrade_refuses_a_literal_it_cannot_rewrite(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE public.ledger_observation_credit DISABLE TRIGGER USER")
        conn.exec_driver_sql("UPDATE public.ledger_observation_credit SET raw = 'null'::jsonb")
        conn.exec_driver_sql("ALTER TABLE public.ledger_observation_credit ENABLE TRIGGER USER")
    engine.dispose()
    with pytest.raises(Exception, match=r"refuse to rewrite public\.ledger_observation_credit\.raw"):
        alembic(url, "upgrade", "head")


def test_an_attempt_names_exactly_one_of_policy_and_seed(seeded) -> None:  # noqa: F811
    with seeded.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE public.submission_attempt_journal DISABLE TRIGGER USER")
        assert conn.exec_driver_sql(
            "SELECT count(*) FROM public.submission_attempt_journal "
            "WHERE policy_revision_id IS NOT NULL").scalar()
    for assignment in ("seed_provenance = '{}'::jsonb", "policy_revision_id = NULL"):
        with seeded.begin() as conn, pytest.raises(
                Exception, match="ck_submission_attempt_policy_or_seed"):
            conn.exec_driver_sql("ALTER TABLE public.submission_attempt_journal DISABLE TRIGGER USER")
            conn.exec_driver_sql(f"UPDATE public.submission_attempt_journal SET {assignment}")
