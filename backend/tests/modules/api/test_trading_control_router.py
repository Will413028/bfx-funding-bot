"""Web API for approve/resume: it queues a request and changes nothing else."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core import auth
from bfx_funding_bot.core.auth import Principal
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountMembership
from bfx_funding_bot.modules.execution.safety.tables import (
    DeploymentApprovalRow,
    TradingControlRequestRow,
    TradingStateRow,
)

DIGEST = "sha256:" + "a" * 64
AUTH = {"Authorization": "Bearer fixture"}


async def _app(sqlite_engine, monkeypatch, *, role="owner", principal=None):
    from bfx_funding_bot.modules.api.trading_control import build_trading_control_router
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator")
    monkeypatch.setenv("BFX_IMAGE_DIGEST", DIGEST)
    monkeypatch.setenv("BFX_SOURCE_REVISION", "c" * 40)
    monkeypatch.setenv("BFX_CHANGE_CLASS", "material")
    monkeypatch.setattr(auth, "_verify", lambda _: principal or Principal("operator", None, "admin"))
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="fixture"))
        await session.flush()
        session.add(ExchangeAccountMembership(exchange_account_id=account, user_id="operator", role=role))
    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(build_trading_control_router())
    return app, factory, account


@pytest.mark.asyncio
async def test_approve_only_queues_a_request_with_the_operator_identity(sqlite_engine, monkeypatch):
    app, factory, account = await _app(sqlite_engine, monkeypatch)
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"{base}/approve", json={"backend_digest": DIGEST,
                                                             "reason": "reviewed the diff"}, headers=AUTH)
        assert response.status_code == 202
        request_id = response.json()["data"]["request_id"]
        again = await client.post(f"{base}/resume", json={"backend_digest": DIGEST, "reason": "x"},
                                  headers=AUTH)
        assert again.status_code == 409 and again.json()["detail"] == "request_pending"
        status = await client.get(f"{base}/requests/{request_id}", headers=AUTH)
        assert status.json()["data"]["state"] == "requested"
        overview = (await client.get(base, headers=AUTH)).json()["data"]
        assert overview["running"]["backend_digest"] == DIGEST
        assert overview["trading_state"] is None
    async with factory() as session:
        rows = (await session.scalars(select(TradingControlRequestRow))).all()
        assert [(r.action, r.requested_by, r.state) for r in rows] == [("approve", "operator", "requested")]
        # Nothing the daemon owns was written.
        assert (await session.scalars(select(TradingStateRow))).all() == []
        assert (await session.scalars(select(DeploymentApprovalRow))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "status"), [
    ({"backend_digest": "sha256:short", "reason": "x"}, 422),
    ({"backend_digest": DIGEST, "reason": ""}, 422),
    ({"backend_digest": DIGEST, "reason": "x", "state": "applied"}, 422),
])
async def test_malformed_or_forged_requests_are_refused(sqlite_engine, monkeypatch, body, status):
    app, _, account = await _app(sqlite_engine, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/v1/exchange-accounts/{account}/trading-control/approve",
                                     json=body, headers=AUTH)
    assert response.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "principal", "status"), [
    ("viewer", None, 403),
    ("owner", Principal("other", None, "admin"), 403),
    ("owner", Principal("operator", None, "user"), 403),
])
async def test_only_the_operator_with_write_membership_can_request(sqlite_engine, monkeypatch, role,
                                                                   principal, status):
    app, factory, account = await _app(sqlite_engine, monkeypatch, role=role, principal=principal)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/v1/exchange-accounts/{account}/trading-control/resume",
                                     json={"backend_digest": DIGEST, "reason": "x"}, headers=AUTH)
    assert response.status_code == status
    async with factory() as session:
        assert (await session.scalars(select(TradingControlRequestRow))).all() == []
