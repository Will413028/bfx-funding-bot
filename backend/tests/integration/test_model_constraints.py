"""The models carry the same CHECK constraints the migrations create.

PostgreSQL is the rules' authority, and Alembic does not compare CHECKs. So
create the governance tables from the models beside the migrated ones and compare
PostgreSQL's own rendering of every CHECK, by table and name.
"""
import pytest
from sqlalchemy import text

import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.deployments.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.capital_tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.execution.safety.tables
import bfx_funding_bot.modules.execution.uncertainty_tables  # noqa: F401
from bfx_funding_bot.core.db import Base

pytestmark = pytest.mark.integration

GOVERNANCE_TABLES = ("trading_state", "funding_cancel_all_audit", "deployments",
                     "trading_control_requests", "uncertainty_resolution_requests",
                     "capital_policy_requests")
# 5b1e7c9d2a40 left these NOT VALID so rows recorded under the retired rules
# stay as they were; the model states the rule every new row obeys.
_NOT_VALID = {("trading_state", "ck_trading_state_state"), ("trading_state", "ck_trading_state_cause")}

_CHECKS = text("""SELECT t.relname, c.conname, pg_get_constraintdef(c.oid)
    FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE c.contype = 'c' AND n.nspname = :schema AND t.relname = ANY(:tables)""")


@pytest.mark.asyncio
async def test_models_and_migrations_define_the_same_checks(migrated_db):
    factory, _ = migrated_db
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE SCHEMA model_side")
        await conn.run_sync(lambda sync: Base.metadata.create_all(
            sync.execution_options(schema_translate_map={None: "model_side"})))
    async with engine.connect() as conn:
        migrated = {(r[0], r[1]): r[2] for r in await conn.execute(
            _CHECKS, {"schema": "public", "tables": list(GOVERNANCE_TABLES)})}
        modeled = {(r[0], r[1]): r[2] for r in await conn.execute(
            _CHECKS, {"schema": "model_side", "tables": list(GOVERNANCE_TABLES)})}
    assert migrated, "no governance CHECKs found"
    for key in _NOT_VALID:
        assert migrated[key].endswith(" NOT VALID"), key
        migrated[key] = migrated[key].removesuffix(" NOT VALID")
    assert modeled == migrated
