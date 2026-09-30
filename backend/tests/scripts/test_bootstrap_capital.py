from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

import bfx_funding_bot.apps.bot  # noqa: F401
from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.capital_repository import (
    CapitalBlockedError,
    CapitalRepository,
)
from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.protocols import Credentials
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from bfx_funding_bot.modules.ledger.tables import CapitalPolicyRevisionRow
from tests.modules.accounts.test_capital_conversion import convert_capital_policy, legacy


class ReadOnlyVenue:
    def __init__(self, *, include_usd=True):
        self.calls = []
        self.wallets = {"fUST": Decimal("1000.123456789")}
        if include_usd:
            self.wallets["fUSD"] = Decimal("0")

    async def get_active_funding_offers(self, **kwargs):
        self.calls.append("offers")
        assert kwargs.get("symbol") is None
        return []

    async def get_active_funding_loans(self, **kwargs):
        # Lent but not yet drawn into a position; none in this fixture.
        return []

    async def get_active_funding_credits(self, **kwargs):
        self.calls.append("credits")
        assert kwargs.get("symbol") is None
        return []

    async def get_funding_available_all(self, **kwargs):
        self.calls.append("wallets")
        return self.wallets.copy()

    async def submit(self, *args, **kwargs):
        pytest.fail("bootstrap must never submit")

    async def cancel(self, *args, **kwargs):
        pytest.fail("bootstrap must never cancel")


@pytest.mark.integration
@pytest.mark.parametrize("include_usd", [False, True], ids=["ust_only", "both_wallets"])
async def test_first_deployment_snapshot_breaks_conversion_cycle_without_policy_seed(
    pg_session_factory, pg_container, include_usd, monkeypatch,
):
    from scripts.bootstrap_capital import bootstrap_snapshot

    factory, account = pg_session_factory, uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="first-deploy-fixture"))
    repo = CapitalRepository(account_id=account, environment="ci", max_snapshot_age_ms=300000)
    halt = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    epoch = (await halt.transition("HALTED", cause="operator", reason="fixture", actor="fixture")).state
    venue = ReadOnlyVenue(include_usd=include_usd)
    preview_cells = []
    preview = CapitalRepository.preview_policy

    async def track_preview(self, session, **kwargs):
        preview_cells.append((kwargs["symbol"], kwargs["cell_id"]))
        return await preview(self, session, **kwargs)

    monkeypatch.setattr(CapitalRepository, "preview_policy", track_preview)
    async def credentials(session):
        return Credentials("fixture-key", "fixture-secret")
    receipt = await bootstrap_snapshot(database_url=pg_container.get_connection_url().replace("+psycopg2", "+asyncpg"),
        account_id=account, environment="ci", venue=venue, credentials=credentials, clock=lambda: 1100)
    assert receipt["status"] == "snapshot_ready"
    assert receipt["resumed"] is False
    assert receipt["policies_applied"] is False
    assert preview_cells == [("fUST", "fUST_a30")]
    assert receipt["trading_state_id"] == epoch.id
    assert receipt["snapshot_seq"] > 0
    assert venue.calls == ["offers", "credits", "wallets"] * 2
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == 0
        stored = await session.get(CapitalSnapshotRow, receipt["snapshot_seq"])
        assert set(stored.classification["symbols"]) == set(venue.wallets)
        logged = await session.get(EventLogRow, receipt["snapshot_seq"])
        assert set(logged.payload["wallet_available"]) == set(venue.wallets)
        dry_run = await convert_capital_policy(session, repository=repo, legacy=legacy(), now_ms=1100, apply_digest=None)
        assert dry_run["symbols"]["fUST"]["cells"]["fUST_a30"]["available"] == "1000.123456789"
        assert dry_run["symbols"]["fUSD"]["cells"]["fUSD_a30"] == {
            "status": "disabled", "capital_evaluated": False,
        }
    async with factory.begin() as session:
        applied = await convert_capital_policy(session, repository=repo, legacy=legacy(), now_ms=1100,
            apply_digest=dry_run["conversion_digest"])
        assert applied["status"] == "applied"
        assert (await repo.read_applied(session, symbol="fUSD")).policy.enabled is False
    assert (await halt.current()).id == epoch.id
    assert (await halt.current()).state == "HALTED"


@pytest.mark.integration
async def test_bootstrap_rejects_missing_enabled_wallet_without_policy_or_resume(pg_session_factory, pg_container):
    from scripts.bootstrap_capital import bootstrap_snapshot

    factory, account = pg_session_factory, uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="missing-ust-fixture"))
    halt = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    epoch = (await halt.transition("HALTED", cause="operator", reason="fixture", actor="fixture")).state
    venue = ReadOnlyVenue()
    del venue.wallets["fUST"]

    async def credentials(session):
        return Credentials("fixture-key", "fixture-secret")

    with pytest.raises(CapitalBlockedError, match="snapshot_symbol_missing"):
        await bootstrap_snapshot(database_url=pg_container.get_connection_url().replace("+psycopg2", "+asyncpg"),
            account_id=account, environment="ci", venue=venue, credentials=credentials, clock=lambda: 1100)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == 0
    assert (await halt.current()).id == epoch.id
    assert (await halt.current()).state == "HALTED"


@pytest.mark.integration
@pytest.mark.parametrize("blocked_by", ["writer", "missing_halt", "not_halted"])
async def test_bootstrap_requires_exclusive_writer_and_existing_halt(pg_session_factory, pg_container, blocked_by):
    from scripts.bootstrap_capital import bootstrap_snapshot

    account = uuid4()
    url = pg_container.get_connection_url().replace("+psycopg2", "+asyncpg")
    async with pg_session_factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="blocked-fixture"))
    halt = TradingStateRepository(pg_session_factory, account_id=account, deployment_environment="ci")
    if blocked_by != "missing_halt":
        await halt.transition("HALTED" if blocked_by == "writer" else "ACTIVE", cause="operator",
                              reason="fixture", actor="fixture")
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
