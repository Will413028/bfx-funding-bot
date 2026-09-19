from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

import bfx_funding_bot.modules.marketfeed.daemon  # noqa: F401
from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRevisionRow
from bfx_funding_bot.modules.execution.protocols import Credentials
from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from tests.modules.accounts.test_capital_conversion import legacy


class ReadOnlyVenue:
    def __init__(self):
        self.calls = []

    async def get_active_funding_offers(self, **kwargs):
        self.calls.append("offers")
        assert kwargs.get("symbol") is None
        return []

    async def get_active_funding_credits(self, **kwargs):
        self.calls.append("credits")
        assert kwargs.get("symbol") is None
        return []

    async def get_funding_available_all(self, **kwargs):
        self.calls.append("wallets")
        return {"fUST": Decimal("1000.123456789"), "fUSD": Decimal("0")}

    async def submit(self, *args, **kwargs):
        pytest.fail("bootstrap must never submit")

    async def cancel(self, *args, **kwargs):
        pytest.fail("bootstrap must never cancel")


@pytest.mark.integration
async def test_first_deployment_snapshot_breaks_conversion_cycle_without_policy_seed(pg_session_factory, pg_container):
    from scripts.bootstrap_capital import bootstrap_snapshot

    factory, account = pg_session_factory, uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="first-deploy-fixture"))
    repo = CapitalRepository(account_id=account, environment="ci", max_snapshot_age_ms=300000)
    halt = HaltStateStore(factory, account_id=str(account), deployment_environment="ci")
    epoch = await halt.set_halted(True, reason="fixture", actor="fixture")
    venue = ReadOnlyVenue()
    async def credentials(session):
        return Credentials("fixture-key", "fixture-secret")
    receipt = await bootstrap_snapshot(database_url=pg_container.get_connection_url().replace("+psycopg2", "+asyncpg"),
        account_id=account, environment="ci", venue=venue, credentials=credentials, clock=lambda: 1100)
    assert receipt["halt_id"] == epoch.id
    assert receipt["snapshot_seq"] > 0
    assert venue.calls == ["offers", "credits", "wallets"] * 2
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == 0
        dry_run = await convert_capital_policy(session, repository=repo, legacy=legacy(), now_ms=1100, apply_digest=None)
        assert dry_run["symbols"]["fUST"]["cells"]["a30"]["available"] == "1000.123456789"
    async with factory.begin() as session:
        applied = await convert_capital_policy(session, repository=repo, legacy=legacy(), now_ms=1100,
            apply_digest=dry_run["conversion_digest"])
        assert applied["status"] == "applied"
        assert (await repo.read_applied(session, symbol="fUSD")).policy.enabled is False
    assert (await halt.current()).id == epoch.id
    assert (await halt.current()).halted


@pytest.mark.integration
@pytest.mark.parametrize("blocked_by", ["writer", "missing_halt", "not_halted"])
async def test_bootstrap_requires_exclusive_writer_and_existing_halt(pg_session_factory, pg_container, blocked_by):
    from scripts.bootstrap_capital import bootstrap_snapshot

    account = uuid4()
    url = pg_container.get_connection_url().replace("+psycopg2", "+asyncpg")
    async with pg_session_factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="blocked-fixture"))
    halt = HaltStateStore(pg_session_factory, account_id=str(account), deployment_environment="ci")
    if blocked_by != "missing_halt":
        await halt.set_halted(blocked_by == "writer", reason="fixture", actor="fixture")
    lock = WriterLock(database_url=url, key=derive_lock_key(str(account), "ci"))
    if blocked_by == "writer":
        await lock.acquire()
    venue = ReadOnlyVenue()
    async def credentials(session):
        pytest.fail("blocked bootstrap must not load credentials")
    try:
        with pytest.raises(Exception, match=r"writer|halt|advisory"):
            await bootstrap_snapshot(database_url=url, account_id=account, environment="ci",
                venue=venue, credentials=credentials)
        assert venue.calls == []
    finally:
        await lock.release()
