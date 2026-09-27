import pytest

from tests.pg_templates import alembic

pytestmark = pytest.mark.integration


def test_alembic_check_reports_no_drift_after_upgrade(pg_head_url) -> None:
    """Run the project autogenerate check against a real PostgreSQL schema.

    ``pg_head_url`` is a fresh copy of the database Alembic migrated from empty
    to head.
    """
    alembic(pg_head_url, "check")
