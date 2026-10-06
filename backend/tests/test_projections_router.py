"""SP4 projection read endpoints — positions / offers / executions.

The router's own contract: the wire shape of the read models' views, the scope and states it
asks them for, the cursor and limit validation, and the operator gate. Positions and offers
come from a recording stand-in for the ledger's ``OperatorReads`` (its SQL is covered on
PostgreSQL by ``tests/integration/test_ledger_positions_offers.py``); executions read the
archived legacy ``event_log`` (the history below the switch) on SQLite.
"""
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

# Every table the shared metadata may reach by foreign key, whatever was imported first.
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.execution.uncertainty_tables  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_operator, require_user
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.api.deps import ReadModels, get_session
from bfx_funding_bot.modules.api.projections import _ACTIVE_CLAIM_STATES, build_projections_router
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.execution.archived_execution_history import (
    ArchivedExecutionHistory,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.ledger import OfferView, PositionView, Scope

_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_ACC = "default"
_ENV = "prod"
_POSITIONS_PATH = f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/positions"
_OFFERS_PATH = f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/offers"
_EXECUTIONS_PATH = f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/executions"


_SCOPE = Scope(_ACCOUNT_ID, _ENV)


class _Reads:
    """The positions and offers the router renders, and what it asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Scope, tuple[str, ...] | None]] = []

    async def list_positions(self, session, scope):
        self.calls.append(("positions", scope, None))
        return (
            PositionView("fUSD", Decimal("0"), Decimal("0"), Decimal("0"), None, 0, 1000, None),
            PositionView("fUST", Decimal("11.5"), Decimal("22.25"), Decimal("2314.03"),
                         Decimal("4"), 3, 1000, 2000),
        )

    async def list_offers(self, session, scope, *, states):
        self.calls.append(("offers", scope, tuple(states)))
        return (
            OfferView("2", None, "pending", "fUST", Decimal("100"), 1002, 2002),
            OfferView("1", "v1", "claimed", "fUST", Decimal("100"), 1001, 2001),
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
        for i, etype in enumerate(
            ["RESERVATION_INTENT", "RESERVATION_CLAIMED", "ORDER_FILL", "CREDIT_CLOSED"]
        ):
            s.add(_event(etype, ts=10_000 + i))
        s.add(_event("ORDER_FILL", ts=99_999, env="canary"))  # other realm
        await s.commit()
    return factory


def _models(reads: _Reads) -> ReadModels:
    unused = object()
    return ReadModels(reads, unused, unused, ArchivedExecutionHistory(), unused)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def reads() -> _Reads:
    return _Reads()


@pytest_asyncio.fixture
async def app_client(factory, reads, monkeypatch):
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    app = FastAPI()
    app.include_router(build_projections_router())
    app.state.read_models = _models(reads)

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
    app.state.read_models = _models(_Reads())

    async def _override_session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


def test_positions_realm_scoped_camel_case(app_client, reads):
    resp = app_client.get(_POSITIONS_PATH)
    assert resp.status_code == 200
    assert reads.calls == [("positions", _SCOPE, None)]
    data = resp.json()["data"]
    assert [p["symbol"] for p in data] == ["fUSD", "fUST"]  # in the read model's order
    fust = data[1]
    # Disjoint components straight from the view; no reserved/realized.
    assert (fust["available"], fust["offered"], fust["lent"]) == ("11.5", "22.25", "2314.03")
    assert fust["unattributedLent"] == "4"
    assert data[0]["unattributedLent"] is None and data[0]["lastReconciledAtMs"] is None
    assert "reserved" not in fust and "realized" not in fust
    assert fust["nCredits"] == 3
    assert fust["lastReconciledAtMs"] == 2000  # *Ms suffix (contract v2 rename)
    assert "lastReconciledAt" not in fust


def test_position_responses_omit_event_seq_fields(app_client):
    response = app_client.get(_POSITIONS_PATH)
    assert response.status_code == 200
    positions = response.json()["data"]
    assert positions
    for position in positions:
        assert "lastEventSeq" not in position
        assert "last_event_seq" not in position


def test_offers_default_active_only_desc(app_client, reads):
    resp = app_client.get(_OFFERS_PATH)
    assert resp.status_code == 200
    assert reads.calls == [("offers", _SCOPE, tuple(_ACTIVE_CLAIM_STATES))]
    data = resp.json()["data"]
    assert [o["offerKey"] for o in data] == ["2", "1"]  # in the read model's order
    assert data[0]["state"] == "pending" and data[0]["venueOfferId"] is None
    assert all("cid" not in o for o in data)
    assert data[1]["venueOfferId"] == "v1"
    assert data[1]["sizeUsdt"] == "100"


def test_offers_state_filter(app_client, reads):
    resp = app_client.get(_OFFERS_PATH, params={"state": "released"})
    assert resp.status_code == 200
    assert reads.calls == [("offers", _SCOPE, ("released",))]


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
    # Opaque string tokens (ADR 2026-10-02 D4); in the archive the event_seq as text.
    assert isinstance(before, str) and before == data[-1]["eventKey"]

    resp2 = app_client.get(_EXECUTIONS_PATH, params={"limit": 2, "before": before})
    body2 = resp2.json()
    assert len(body2["data"]) == 2
    assert int(body2["data"][0]["eventKey"]) < int(before)
    assert [e["eventType"] for e in body2["data"]] == ["RESERVATION_CLAIMED", "RESERVATION_INTENT"]
    # 4 realm rows total -> second page exhausts them
    assert body2["pagination"]["hasMore"] is False
    assert body2["pagination"]["nextBefore"] is None


def test_executions_event_type_filter(app_client):
    resp = app_client.get(_EXECUTIONS_PATH, params={"event_type": "ORDER_FILL"})
    assert resp.status_code == 200
    body = resp.json()
    assert [e["eventType"] for e in body["data"]] == ["ORDER_FILL"]  # canary row excluded
    assert body["pagination"]["hasMore"] is False


def test_executions_refuse_a_cursor_they_did_not_issue(app_client):
    for cursor in ("j.MTox", "abc", "-1"):
        resp = app_client.get(_EXECUTIONS_PATH, params={"before": cursor})
        assert (resp.status_code, resp.json()["detail"]) == (422, "invalid_cursor"), cursor


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
