from decimal import Decimal

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.modules.accounts.tables import AccountConfigDraft
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRevisionRow
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow
from tests.integration.test_capital_repository import (
    capital_db,
    capital_engine,
    repository,
    snapshot,
)

# Re-export real DB fixtures; PG variants retain the integration marker.
__all__ = ["capital_db", "capital_engine"]


def legacy():
    return {"schema_version": 1, "caps": {"fUST": "200", "fUSD": "0"},
            "default_cap": "0", "env_fallback_cap": "400",
            "buffers": {"fUST": "3", "fUSD": "0"}, "default_buffer": "0",
            "env_fallback_buffer": "3", "max_cell_fraction": "0.70"}


@pytest.mark.asyncio
async def test_preview_apply_preserves_draft_and_history_and_never_resumes(capital_db):
    from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
    factory, account = capital_db
    repo = repository(account)
    await snapshot(factory, repo)
    async with factory.begin() as session:
        session.add(AccountConfigDraft(exchange_account_id=account, config={"currency": "UST"},
                                       revision=7, source="operator"))
        session.add(TradingHaltRow(account_id=str(account), exchange_account_id=account,
            deployment_environment="ci", halted=True, reason="test", actor="test", created_at_ms=1))
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=legacy(),
                                              now_ms=1100, apply_digest=None)
        assert report["symbols"]["fUST"]["new_policy"]["reserve_amount"] == "0"
        assert report["symbols"]["fUSD"]["new_policy"]["enabled"] is False
        assert report["symbols"]["fUST"]["old_effective"]["cap"] == "200"
        assert report["symbols"]["fUST"]["cells"]["a30"]["new_max_new_offer"] == "700.00"
        assert report["symbols"]["fUST"]["cells"]["a30"]["delta"] == "500.00"
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == 0
    async with factory.begin() as session:
        applied = await convert_capital_policy(session, repository=repo, legacy=legacy(),
            now_ms=1100, apply_digest=report["conversion_digest"])
        assert applied["status"] == "applied"
        assert (await repo.read_applied(session, symbol="fUST")).policy.reserve_amount == Decimal("0")
        assert (await session.scalar(select(AccountConfigDraft))).revision == 7
        assert (await session.scalar(select(TradingHaltRow))).halted is True
    async with factory.begin() as session:
        again = await convert_capital_policy(session, repository=repo, legacy=legacy(),
            now_ms=1100, apply_digest=report["conversion_digest"])
        assert again["status"] == "already_applied"
        assert await session.scalar(select(func.count()).select_from(CapitalPolicyRevisionRow)) == 2


@pytest.mark.asyncio
async def test_invalid_legacy_and_stale_draft_do_not_apply(capital_db):
    from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    factory, account = capital_db
    repo = repository(account)
    await snapshot(factory, repo)
    bad = legacy()
    bad["caps"] = {"fUST": "NaN"}
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=bad,
                                              now_ms=1100, apply_digest=None)
        assert report["invalid_items"] == ["caps.fUST"]
        assert report["status"] == "invalid"
    async with factory.begin() as session:
        report = await convert_capital_policy(session, repository=repo, legacy=legacy(),
                                              now_ms=1100, apply_digest=None)
        session.add(AccountConfigDraft(exchange_account_id=account, config={"currency": "USD"},
                                       revision=1, source="operator"))
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="conversion_changed"):
            await convert_capital_policy(session, repository=repo, legacy=legacy(),
                now_ms=1100, apply_digest=report["conversion_digest"])
