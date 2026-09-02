from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.integration

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"
_REVISION = "bc4d5e6f7081"


def _reset_and_upgrade(sync_url: str) -> None:
    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command

    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
    finally:
        engine.dispose()
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")


@pytest.mark.asyncio
async def test_serialized_projector_schema_contract(pg_engine, monkeypatch) -> None:
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_and_upgrade(sync_url)

    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(sync_url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names(schema="public"))
            assert {"projection_heads", "venue_offer_state", "venue_credit_state"} <= tables

            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == _REVISION

            event_columns = {
                column["name"]: column for column in inspector.get_columns("event_log")
            }
            assert event_columns["event_id"]["nullable"] is True
            assert event_columns["schema_version"]["nullable"] is False
            event_indexes = {
                index["name"]: index for index in inspector.get_indexes("event_log")
            }
            assert event_indexes["uq_event_log_event_id"]["unique"] is True

            position_columns = {
                column["name"] for column in inspector.get_columns("position_state")
            }
            assert {
                "offered_amount",
                "lent_amount",
                "available_amount",
                "uncertain_amount",
                "last_venue_snapshot_at",
            } <= position_columns

            for table, identity in (
                ("venue_offer_state", {"exchange_account_id", "deployment_environment", "venue_offer_id"}),
                ("venue_credit_state", {"exchange_account_id", "deployment_environment", "credit_id"}),
            ):
                pk = set(inspector.get_pk_constraint(table)["constrained_columns"])
                assert pk == identity
                foreign_keys = inspector.get_foreign_keys(table)
                assert foreign_keys
                assert all(
                    fk["options"].get("ondelete") == "RESTRICT"
                    for fk in foreign_keys
                )
                columns = {
                    column["name"]: column for column in inspector.get_columns(table)
                }
                assert columns["flags"]["default"] is not None
                assert columns["is_terminal"]["default"] is not None

            check_constraints = inspector.get_check_constraints("event_log")
            assert any(
                constraint["name"] == "ck_event_log_v3_event_id"
                for constraint in check_constraints
            )
    finally:
        engine.dispose()
