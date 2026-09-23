"""Status adapter membership queries under the deployed restricted webapi ACL."""
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

import bfx_funding_bot.modules.execution.capital_tables  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountMembership
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.funding_status import build_funding_status_router


@pytest.mark.integration
async def test_status_membership_uses_only_existing_restricted_webapi_grants(pg_session_factory, monkeypatch):
    factory, account, other = pg_session_factory, uuid4(), uuid4()
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    async with factory.begin() as session:
        await session.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN CREATE ROLE bfx_webapi; END IF; END $$"))
        await session.execute(text("ALTER ROLE bfx_webapi NOSUPERUSER NOCREATEROLE NOINHERIT"))
        await session.execute(text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM bfx_webapi"))
        await session.execute(text("GRANT USAGE ON SCHEMA public TO bfx_webapi"))
        # Exact pre-existing membership read grants from halt-1 cutover runbook.
        # No capital/auth table or ownership privileges are supplied to this reader.
        await session.execute(text("GRANT SELECT ON exchange_accounts,exchange_account_memberships TO bfx_webapi"))
        session.add_all([ExchangeAccount(id=a, venue="bitfinex", label="acl-fixture") for a in (account, other)])
        await session.flush()
        session.add(ExchangeAccountMembership(exchange_account_id=account, user_id="fixture", role="owner"))
    calls = []
    daemon_realm = "ci"
    scoped_endpoint = "/admin/trading-status"
    async def reader(path):
        calls.append(path)
        value = {"account_id": str(account), "deployment_environment": "ci",
            "symbols": {"fUST": {"capital_available": False, "reason": "policy_unavailable"}}}
        if path == scoped_endpoint:
            if daemon_realm is None:
                value.pop("deployment_environment")
            else:
                value["deployment_environment"] = daemon_realm
        return value
    async def restricted_session():
        async with factory() as session:
            await session.execute(text("SET LOCAL ROLE bfx_webapi"))
            assert await session.scalar(text("SELECT current_user")) == "bfx_webapi"
            assert not await session.scalar(text("SELECT has_table_privilege(current_user,'capital_policy_heads','SELECT')"))
            yield session
    app = FastAPI()
    app.include_router(build_funding_status_router(reader=reader))
    app.dependency_overrides[require_operator] = lambda: Principal("fixture", None, "admin")
    app.dependency_overrides[get_session] = restricted_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(f"/api/v1/exchange-accounts/{account}/funding-status")
        assert ok.status_code == 200
        assert ok.json()["data"]["symbols"]["fUST"]["reason"] == "policy_unavailable"
        denied = await client.get(f"/api/v1/exchange-accounts/{other}/funding-status")
        assert denied.status_code == 404
        assert calls == ["/admin/trading-status", "/admin/dry-evaluate"]
        for scoped_endpoint in ("/admin/trading-status", "/admin/dry-evaluate"):
            for realm in ("prod", None):
                daemon_realm = realm
                calls.clear()
                mismatch = await client.get(f"/api/v1/exchange-accounts/{account}/funding-status")
                assert mismatch.status_code == 503
                assert "symbols" not in mismatch.json()
                assert len(calls) == (1 if scoped_endpoint.endswith("trading-status") else 2)
