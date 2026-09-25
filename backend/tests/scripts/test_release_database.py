from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

import bfx_funding_bot.modules.marketfeed.daemon  # noqa: F401
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.release_worker import RELEASE_SCHEMA_HEAD
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository


@pytest.mark.integration
async def test_deployment_requires_explicit_applied_policies_and_preserves_halt(pg_session_factory):
    from scripts.release_database import deployment_readiness
    account = uuid4()
    factory = pg_session_factory
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="deploy-fixture"))
        await session.execute(text("CREATE TABLE alembic_version(version_num varchar(32))"))
        await session.execute(text("INSERT INTO alembic_version VALUES (:head)"),
                              {"head": RELEASE_SCHEMA_HEAD})
    halt = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    epoch = (await halt.transition("HALTED", cause="operator", reason="fixture", actor="fixture")).state
    repo = CapitalRepository(account_id=account, environment="ci", max_snapshot_age_ms=300000)
    with pytest.raises(Exception, match="policy_unavailable"):
        await deployment_readiness(factory, account_id=account, environment="ci", expected_principal="test")
    async with factory.begin() as session:
        for symbol in ("fUST", "fUSD"):
            await repo.apply_policy(session, symbol=symbol, expected_revision=0,
                policy=CapitalPolicy(enabled=symbol == "fUST", max_cell_fraction=Decimal("0.700")), source={"fixture": True})
    result = await deployment_readiness(factory, account_id=account, environment="ci", expected_principal="test")
    assert result["trading_state_id"] == epoch.id
    assert result["trading_state"] == "HALTED"
    assert result["halted"] is True
    assert result["policies"]["fUSD"]["enabled"] is False
    assert (await halt.current()).id == epoch.id
    await halt.transition("ACTIVE", cause="operator", reason="fixture", actor="fixture")
    with pytest.raises(Exception, match="halt"):
        await deployment_readiness(factory, account_id=account, environment="ci", expected_principal="test")
