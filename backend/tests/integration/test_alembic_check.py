import pytest

from tests.pg_templates import alembic

pytestmark = pytest.mark.integration


def test_alembic_check_reports_no_drift_after_upgrade(pg_head_url) -> None:
    """Run the project autogenerate check against a real PostgreSQL schema.

    ``pg_head_url`` is a fresh copy of the database Alembic migrated from empty
    to head.
    """
    alembic(pg_head_url, "check")


def test_generated_columns_match_the_model(pg_head_url) -> None:
    """``alembic check`` skips generated columns (``include_object``: Alembic's
    server-default hook cannot read a reflected ``Computed``), so their shape is
    compared here: every model generated column exists as a stored generated
    column with the model's nullability and type."""
    from sqlalchemy import create_engine, text

    import bfx_funding_bot.modules.ledger.tables  # noqa: F401  # registers ledger metadata
    from bfx_funding_bot.core.db import Base

    generated = [
        (table.name, column)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.computed is not None
    ]
    assert generated, "expected at least one generated column in the model"
    engine = create_engine(pg_head_url)
    try:
        with engine.connect() as conn:
            for table_name, column in generated:
                row = conn.execute(
                    text(
                        "SELECT is_nullable, data_type, is_generated FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
                    ),
                    {"t": table_name, "c": column.name},
                ).one()
                expected_type = column.type.compile(dialect=conn.dialect).lower()
                assert row.is_generated == "ALWAYS", (table_name, column.name)
                assert (row.is_nullable == "YES") is column.nullable, (table_name, column.name)
                assert row.data_type == expected_type, (table_name, column.name, row.data_type)
    finally:
        engine.dispose()
