import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core import auth
from bfx_funding_bot.core.auth import Principal
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountMembership


@pytest.mark.asyncio
@pytest.mark.parametrize("principal,member_role,status", [
    (Principal("operator", None, "admin"), "owner", 200),
    (Principal("other", None, "admin"), "owner", 403),
    (Principal("operator", None, "user"), "owner", 403),
    (Principal("operator", None, "admin"), None, 404),
    (Principal("operator", None, "admin"), "viewer", 403),
])
async def test_authenticated_session_request_is_scoped_and_cannot_promote(sqlite_engine, monkeypatch,
                                                                         principal, member_role, status):
    from bfx_funding_bot.modules.api.release_sessions import build_release_router
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator")
    monkeypatch.setattr(auth, "_verify", lambda _: principal)
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="fixture"))
        await session.flush()
        if member_role:
            session.add(ExchangeAccountMembership(exchange_account_id=account, user_id="operator", role=member_role))
    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(build_release_router())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/v1/exchange-accounts/{account}/release-sessions"
        body = {"symbol": "fUST", "cell": "fUST_a30", "strategy": "mean_reversion",
                "max_amount": "200", "expires_at_ms": int(time.time() * 1000) + 60000}
        response = await client.post(path, json=body, headers={"Authorization": "Bearer fixture"})
        assert response.status_code == status
        if status == 200:
            data = response.json()["data"]
            assert data["state"] == "requested"
            assert data["binding"] is None
            promote = await client.post(path + "/" + data["id"] + "/promote",
                json={"expected_revision": 1}, headers={"Authorization": "Bearer fixture"})
            assert promote.status_code == 409
            forged = await client.post(path, json={**body, "state": "promoted", "binding": {}},
                                       headers={"Authorization": "Bearer fixture"})
            assert forged.status_code == 422
