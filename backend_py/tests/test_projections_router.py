"""SP4 projection read endpoints — positions / offers / executions.

Read-only over position_state / offer_claims / event_log, realm-scoped to the
BFX_ACCOUNT_ID / BFX_DEPLOYMENT_ENV env (operator console v1: single bot
account), every route behind require_operator.
"""
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_operator, require_user
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.projections import build_projections_router
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)

_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_ACC = "default"
_ENV = "prod"
_POSITIONS_PATH = f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/positions"
_OFFERS_PATH = f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/offers"
_EXECUTIONS_PATH = f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/executions"


def _position(symbol: str, realized: str, account: str = _ACC, env: str = _ENV) -> PositionStateRow:
    return PositionStateRow(
        account_id=account, deployment_environment=env, symbol=symbol,
        exchange_account_id=_ACCOUNT_ID,
        reserved=Decimal("0"), realized=Decimal(realized),
        last_updated_ms=1000, last_event_seq=7, last_reconciled_at=2000, n_credits=3,
    )


def _claim(cid: int, state: str, account: str = _ACC, env: str = _ENV) -> OfferClaimRow:
    return OfferClaimRow(
        cid=cid, account_id=account, deployment_environment=env, state=state,
        exchange_account_id=_ACCOUNT_ID,
        venue_offer_id=f"v{cid}", symbol="fUST", size_usdt=Decimal("100"),
        signal_correlation_id="11111111-1111-1111-1111-111111111111",
        occurred_at_ms=1000 + cid, last_updated_ms=2000 + cid, last_event_seq=cid,
    )


def _event(etype: str, ts: int, account: str = _ACC, env: str = _ENV) -> EventLogRow:
    return EventLogRow(
        account_id=account, exchange_account_id=_ACCOUNT_ID,
        deployment_environment=env, event_type=etype,
        venue_offer_id="v1", cid=42,
        payload={"symbol": "fUST", "amount": "123.5", "fill_rate": 0.0002},
        occurred_at_ms=ts,
    )


@pytest_asyncio.fixture
async def factory(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            ExchangeAccount(
                id=_ACCOUNT_ID,
                venue="bitfinex",
                label="Primary",
                lifecycle_status="active",
            )
        )
        await s.flush()
        await grant_membership(
            s,
            exchange_account_id=_ACCOUNT_ID,
            user_id="user_abc",
            role="owner",
        )
        s.add(_position("fUST", "2314.03"))
        s.add(_position("fUSD", "0"))
        s.add(_position("fUST", "999", env="canary"))       # other realm
        s.add(_claim(1, "claimed"))
        s.add(_claim(2, "pending"))
        s.add(_claim(3, "released"))
        s.add(_claim(4, "claimed", env="canary"))           # other realm
        for i, etype in enumerate(
            ["RESERVATION_INTENT", "RESERVATION_CLAIMED", "ORDER_FILL", "CREDIT_CLOSED"]
        ):
            s.add(_event(etype, ts=10_000 + i))
        s.add(_event("ORDER_FILL", ts=99_999, env="canary"))  # other realm
        await s.commit()
    return factory


@pytest_asyncio.fixture
async def app_client(factory, monkeypatch):
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    app = FastAPI()
    app.include_router(build_projections_router())

    async def _fake_operator():
        return Principal(user_id="user_abc", email="will@example.com", role="operator")

    async def _override_session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[require_operator] = _fake_operator
    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


@pytest_asyncio.fixture
async def anon_client(factory):
    """No require_operator override — the real dependency must reject."""
    app = FastAPI()
    app.include_router(build_projections_router())

    async def _override_session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


def test_positions_realm_scoped_camel_case(app_client):
    resp = app_client.get(_POSITIONS_PATH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [p["symbol"] for p in data] == ["fUSD", "fUST"]  # symbol asc, no canary leak
    fust = data[1]
    assert fust["realized"] == "2314.03"
    assert fust["nCredits"] == 3
    assert fust["lastReconciledAtMs"] == 2000  # *Ms suffix (contract v2 rename)
    assert "lastReconciledAt" not in fust
    assert fust["lastEventSeq"] == 7


def test_offers_default_active_only_desc(app_client):
    resp = app_client.get(_OFFERS_PATH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [o["cid"] for o in data] == [2, 1]  # last_updated_ms desc; released + canary excluded
    assert data[0]["state"] == "pending"
    assert data[1]["venueOfferId"] == "v1"
    assert data[1]["sizeUsdt"] == "100"


def test_offers_state_filter(app_client):
    resp = app_client.get(_OFFERS_PATH, params={"state": "released"})
    assert resp.status_code == 200
    assert [o["cid"] for o in resp.json()["data"]] == [3]


def test_executions_desc_with_limit_and_cursor(app_client):
    resp = app_client.get(_EXECUTIONS_PATH, params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert len(data) == 2
    assert data[0]["eventType"] == "CREDIT_CLOSED"  # newest first, canary excluded
    assert data[0]["amount"] == "123.5"
    assert data[0]["rate"] == 0.0002
    # contract v2: pagination envelope
    assert body["pagination"]["hasMore"] is True
    before = body["pagination"]["nextBefore"]
    assert before == data[-1]["eventSeq"]

    resp2 = app_client.get(_EXECUTIONS_PATH, params={"limit": 2, "before": before})
    body2 = resp2.json()
    assert len(body2["data"]) == 2
    assert body2["data"][0]["eventSeq"] < before
    # 4 realm rows total -> second page exhausts them
    assert body2["pagination"]["hasMore"] is False
    assert body2["pagination"]["nextBefore"] is None


def test_executions_event_type_filter(app_client):
    resp = app_client.get(_EXECUTIONS_PATH, params={"event_type": "ORDER_FILL"})
    assert resp.status_code == 200
    body = resp.json()
    assert [e["eventType"] for e in body["data"]] == ["ORDER_FILL"]  # canary row excluded
    assert body["pagination"]["hasMore"] is False


def test_executions_limit_capped(app_client):
    resp = app_client.get(_EXECUTIONS_PATH, params={"limit": 999})
    assert resp.status_code == 422  # over cap rejected by validation


def test_non_operator_is_rejected_by_every_projection_route(app_client):
    """Catches any projection route wired to require_user instead of require_operator."""
    async def _reject_non_operator():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator_required")

    async def _permissive_user():
        return Principal(user_id="user_abc", email="will@example.com", role="operator")

    async def _skip_rate_limit():
        return None

    app_client.app.dependency_overrides[require_operator] = _reject_non_operator
    app_client.app.dependency_overrides[require_user] = _permissive_user
    app_client.app.dependency_overrides[shared_rate_limit_dependency()] = _skip_rate_limit
    for path in (_POSITIONS_PATH, _OFFERS_PATH, _EXECUTIONS_PATH):
        response = app_client.get(path)
        assert response.status_code == 403, path


def test_all_routes_require_auth(anon_client):
    for path in (_POSITIONS_PATH, _OFFERS_PATH, _EXECUTIONS_PATH):
        resp = anon_client.get(path)
        assert resp.status_code in (401, 403), path
