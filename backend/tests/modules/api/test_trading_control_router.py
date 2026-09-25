"""Web API for approve/resume: it queues a request and changes nothing else."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.audit.tables
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


@pytest.mark.asyncio
async def test_stops_name_no_build_and_a_kill_is_never_blocked(sqlite_engine, monkeypatch):
    app, factory, account = await _app(sqlite_engine, monkeypatch)
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        missing = await client.post(f"{base}/approve", json={"reason": "no digest"}, headers=AUTH)
        assert (missing.status_code, missing.json()["detail"]) == (422, "backend_digest_required")
        pause = await client.post(f"{base}/pause", json={"reason": "maintenance",
                                                         "backend_digest": DIGEST}, headers=AUTH)
        assert pause.status_code == 202
        # A pending pause never makes a kill wait: the kill has its own lane.
        kill = await client.post(f"{base}/kill", json={"reason": "venue incident"}, headers=AUTH)
        assert kill.status_code == 202
        again = await client.post(f"{base}/kill", json={"reason": "twice"}, headers=AUTH)
        assert (again.status_code, again.json()["detail"]) == (409, "request_pending")
        unknown = await client.post(f"{base}/promote", json={"reason": "x"}, headers=AUTH)
        assert unknown.status_code == 422
    async with factory() as session:
        rows = {r.action: r.backend_digest for r in (await session.scalars(select(TradingControlRequestRow))).all()}
    assert rows == {"pause": None, "kill": None}  # a stop is never refused for a build


@pytest.mark.asyncio
async def test_the_overview_shows_probation_progress_and_the_cancel_all_of_the_halt(sqlite_engine, monkeypatch):
    from decimal import Decimal

    from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow
    from bfx_funding_bot.modules.execution.safety.trading_state import (
        Probation,
        TradingStateRepository,
    )
    app, factory, account = await _app(sqlite_engine, monkeypatch)
    repo = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await repo.transition("ACTIVE", cause="operator", actor="operator", reason="approved", now_ms=1,
        probation=Probation.starting(multiplier=Decimal("0.25"), started_at_ms=1,
                                     floor={"fUST": Decimal("150.75")}))
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        data = (await client.get(base, headers=AUTH)).json()["data"]
        probation = data["trading_state"]["probation"]
        assert (probation["multiplier"], probation["floor"]) == ("0.25", {"fUST": "150.75"})
        assert (probation["acknowledged"], probation["required_acknowledged"]) == (0, 3)
        assert probation["required_ms"] == 24 * 3_600_000 and probation["elapsed_ms"] > 0
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
        assert data["trading_state"]["probation"] is None
        # A lone "requested": the call's outcome was never recorded.
        assert [(c["currency"], c["phase"]) for c in data["cancel_all"]] == [
            ("BTC", "requested"), ("USD", "failed"), ("UST", "acknowledged")]
