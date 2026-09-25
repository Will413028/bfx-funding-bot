"""Web API for trading control: it queues a request and changes nothing else.

On the migrated PostgreSQL schema (its CHECKs and triggers are the authority)."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core import auth
from bfx_funding_bot.core.auth import Principal
from bfx_funding_bot.modules.accounts.tables import ExchangeAccountMembership
from bfx_funding_bot.modules.execution.safety.tables import (
    TradingControlRequestRow,
    TradingStateRow,
)

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "a" * 64
AUTH = {"Authorization": "Bearer fixture"}


async def _app(migrated_db, monkeypatch, *, role="owner", principal=None):
    from bfx_funding_bot.modules.api.trading_control import build_trading_control_router
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator")
    monkeypatch.setenv("BFX_IMAGE_DIGEST", DIGEST)
    monkeypatch.setenv("BFX_SOURCE_REVISION", "c" * 40)
    monkeypatch.setattr(auth, "_verify", lambda _: principal or Principal("operator", None, "admin"))
    factory, account = migrated_db
    async with factory.begin() as session:
        session.add(ExchangeAccountMembership(exchange_account_id=account, user_id="operator", role=role))
    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(build_trading_control_router())
    return app, factory, account


@pytest.mark.asyncio
async def test_resume_only_queues_a_request_with_the_operator_identity(migrated_db, monkeypatch):
    app, factory, account = await _app(migrated_db, monkeypatch)
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"{base}/resume", json={"reason": "incident closed"},
                                     headers=AUTH)
        assert response.status_code == 202
        request_id = response.json()["data"]["request_id"]
        again = await client.post(f"{base}/resume", json={"reason": "x"}, headers=AUTH)
        assert again.status_code == 409 and again.json()["detail"] == "request_pending"
        status = await client.get(f"{base}/requests/{request_id}", headers=AUTH)
        assert status.json()["data"]["state"] == "requested"
        overview = (await client.get(base, headers=AUTH)).json()["data"]
        assert overview["running"] == {"backend_digest": DIGEST, "source_revision": "c" * 40}
        assert overview["trading_state"] is None
        assert "approvals" not in overview
    async with factory() as session:
        rows = (await session.scalars(select(TradingControlRequestRow))).all()
        assert [(r.action, r.requested_by, r.state) for r in rows] == [("resume", "operator", "requested")]
        # Nothing the daemon owns was written.
        assert (await session.scalars(select(TradingStateRow))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "body", "status"), [
    ("resume", {"reason": ""}, 422),
    ("resume", {"reason": "x", "state": "applied"}, 422),
    ("resume", {"reason": "x", "backend_digest": DIGEST}, 422),   # no build is named any more
    ("approve", {"reason": "x"}, 422),                             # retired actions
    ("pause", {"reason": "x"}, 422),
    ("promote", {"reason": "x"}, 422),
])
async def test_malformed_forged_or_retired_requests_are_refused(migrated_db, monkeypatch, action,
                                                                body, status):
    app, factory, account = await _app(migrated_db, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/v1/exchange-accounts/{account}/trading-control/{action}",
                                     json=body, headers=AUTH)
    assert response.status_code == status
    async with factory() as session:
        assert (await session.scalars(select(TradingControlRequestRow))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "principal", "status"), [
    ("viewer", None, 403),
    ("owner", Principal("other", None, "admin"), 403),
    ("owner", Principal("operator", None, "user"), 403),
])
async def test_only_the_operator_with_write_membership_can_request(migrated_db, monkeypatch, role,
                                                                   principal, status):
    app, factory, account = await _app(migrated_db, monkeypatch, role=role, principal=principal)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/v1/exchange-accounts/{account}/trading-control/resume",
                                     json={"reason": "x"}, headers=AUTH)
    assert response.status_code == status
    async with factory() as session:
        assert (await session.scalars(select(TradingControlRequestRow))).all() == []


@pytest.mark.asyncio
async def test_a_kill_is_never_blocked_by_a_pending_resume(migrated_db, monkeypatch):
    app, factory, account = await _app(migrated_db, monkeypatch)
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resume = await client.post(f"{base}/resume", json={"reason": "back"}, headers=AUTH)
        assert resume.status_code == 202
        # A pending resume never makes a kill wait: the kill has its own lane.
        kill = await client.post(f"{base}/kill", json={"reason": "venue incident"}, headers=AUTH)
        assert kill.status_code == 202
        again = await client.post(f"{base}/kill", json={"reason": "twice"}, headers=AUTH)
        assert (again.status_code, again.json()["detail"]) == (409, "request_pending")
    async with factory() as session:
        actions = sorted(r.action for r in (await session.scalars(select(TradingControlRequestRow))).all())
    assert actions == ["kill", "resume"]


@pytest.mark.asyncio
async def test_the_overview_shows_the_state_and_the_cancel_all_of_the_halt(migrated_db, monkeypatch):
    from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
    app, factory, account = await _app(migrated_db, monkeypatch)
    repo = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await repo.transition("ACTIVE", cause="operator", actor="operator", reason="start", now_ms=1)
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        data = (await client.get(base, headers=AUTH)).json()["data"]
        assert data["trading_state"] == {"id": data["trading_state"]["id"], "state": "ACTIVE",
                                         "cause": "operator", "actor": "operator",
                                         "reason": "start", "at_ms": 1}
        assert data["cancel_all"] == []
        halted = (await repo.transition("HALTED", cause="operator", actor="operator", reason="kill",
                                        now_ms=2)).state
        async with factory.begin() as session:
            for currency, phase in (("UST", "requested"), ("UST", "acknowledged"),
                                    ("USD", "requested"), ("USD", "failed"), ("BTC", "requested")):
                session.add(FundingCancelAllAuditRow(exchange_account_id=account, deployment_environment="ci",
                    trading_state_id=halted.id, attempt_id=uuid4(), currency=currency, phase=phase,
                    detail=None if phase != "failed" else "venue down", actor="operator", occurred_at_ms=3))
        data = (await client.get(base, headers=AUTH)).json()["data"]
        assert data["trading_state"]["state"] == "HALTED"
        # A lone "requested": the call's outcome was never recorded.
        assert [(c["currency"], c["phase"]) for c in data["cancel_all"]] == [
            ("BTC", "requested"), ("USD", "failed"), ("UST", "acknowledged")]
