"""Migration contract on an isolated PostgreSQL container, including runtime ACLs."""
import pytest
from sqlalchemy import create_engine, inspect, text

from tests.pg_templates import alembic, stamp_realm

pytestmark = pytest.mark.integration


def test_capital_upgrade_drift_and_immutable_runtime_evidence(pg_templates, pg_clone):
    # A database of its own: the container's default one exists only after a pg_engine test.
    url = pg_clone(pg_templates.template("empty_database", lambda _url: None))
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS legacy_archive CASCADE")
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
        connection.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN CREATE ROLE bfx_bot; END IF; END $$")
        connection.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_bot")
        connection.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_bot")
        connection.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO bfx_bot")
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    stamp_realm(url, "ci")
    with engine.begin() as connection:
        # The legacy capital tables live in the archive since c2d3e4f5a6b7; the bot holds nothing there.
        assert "capital_snapshots" in inspect(connection).get_table_names(schema="legacy_archive")
        assert connection.scalar(text("SELECT count(*) FROM capital_policy_revisions")) == 0
        for privilege in ("SELECT", "INSERT", "UPDATE"):
            assert not connection.scalar(text(
                f"SELECT has_table_privilege('bfx_bot','legacy_archive.capital_snapshots','{privilege}')"))
        assert not connection.scalar(text("SELECT has_table_privilege('bfx_bot','capital_policy_heads','UPDATE')"))
        connection.exec_driver_sql("INSERT INTO exchange_accounts (id,venue,label) VALUES ('00000000-0000-0000-0000-00000000ca01','bitfinex','migration')")
    # The archive refuses every write, the owner's included (c2d3e4f5a6b7).
    for sql in ("INSERT INTO legacy_archive.capital_snapshot_queries SELECT * FROM "
                "legacy_archive.capital_snapshot_queries WHERE false",
                "UPDATE legacy_archive.capital_snapshot_queries SET command_fence=5",
                "DELETE FROM legacy_archive.capital_snapshot_queries",
                "TRUNCATE legacy_archive.capital_snapshot_queries CASCADE"):
        with engine.begin() as connection, pytest.raises(Exception, match="legacy_archive is frozen"):
            connection.exec_driver_sql(sql)
    # Below c2d3e4f5a6b7 (before the archive and the switch) the capital tables' own
    # immutability is what held a written row.
    from .test_ledger_schema_roles import pre_switch_url
    pre_switch_url(url)
    alembic(url, "downgrade", "b1c2d3e4f5a6")
    with engine.begin() as connection:
        connection.exec_driver_sql("""INSERT INTO capital_snapshot_queries
            (id,exchange_account_id,deployment_environment,command_fence,query_revision,started_at_ms)
            VALUES ('00000000-0000-0000-0000-00000000ca02','00000000-0000-0000-0000-00000000ca01','ci',0,1,1000)""")
    for sql in ("UPDATE capital_snapshot_queries SET command_fence=5",
                "DELETE FROM capital_snapshot_queries", "TRUNCATE capital_snapshot_queries CASCADE"):
        with engine.begin() as connection, pytest.raises(Exception, match="immutable capital"):
            connection.exec_driver_sql(sql)
    engine.dispose()
