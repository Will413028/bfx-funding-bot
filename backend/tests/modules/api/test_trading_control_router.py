"""Web API for trading control: it queues a request and changes nothing else.

On the migrated PostgreSQL schema (its CHECKs and triggers are the authority)."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import bfx_funding_bot.modules.execution.audit.tables  # noqa: F401
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
    from bfx_funding_bot.apps.read_models import build_read_models
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
    app.state.read_models = build_read_models()
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
    ("resume", {"reason": " \t "}, 422),                           # blank, not request_pending
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


# ------------------------------------------------------- currency enable/disable


async def _seed_policies(factory, account):
    from decimal import Decimal

    from bfx_funding_bot.modules.ledger import Scope
    from bfx_funding_bot.modules.ledger.wiring import build_policy_store
    from bfx_funding_bot.modules.trading import CapitalPolicy, OfferEnvelope
    scope = Scope(account, "ci")
    envelope = OfferEnvelope(min_period_days=2, max_period_days=30, max_open_offers=6,
                             rate_floor_ratio=Decimal("0.5"), min_rate_apr=Decimal("0.01"))
    store = build_policy_store(scope)
    async with factory.begin() as session:
        await store.apply_policy(
            session, symbol="fUST", expected_revision=0, source={"t": 1},
            policy=CapitalPolicy(enabled=True, max_offer_amount=Decimal("200"), envelope=envelope))
        await store.apply_policy(session, symbol="fUSD", expected_revision=0,
                                 source={"t": 1}, policy=CapitalPolicy(enabled=False))


@pytest.mark.asyncio
async def test_the_overview_lists_each_currency_policy_and_its_envelope(migrated_db, monkeypatch):
    app, factory, account = await _app(migrated_db, monkeypatch)
    await _seed_policies(factory, account)
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        currencies = (await client.get(base, headers=AUTH)).json()["data"]["currencies"]
    assert currencies == [
        {"symbol": "fUSD", "revision": 1, "policy_error": None, "enabled": False,
         "max_offer_amount": None, "envelope": None, "requests": []},
        {"symbol": "fUST", "revision": 1, "policy_error": None, "enabled": True,
         "max_offer_amount": "200",
         "envelope": {"min_period_days": 2, "max_period_days": 30, "max_open_offers": 6,
                      "rate_floor_ratio": "0.5", "min_rate_apr": "0.01"},
         "requests": []},
    ]


@pytest.mark.asyncio
async def test_an_unreadable_policy_is_that_currencys_policy_error(migrated_db, monkeypatch):
    """The ledger policy store's refusal is shown as the currency's ``policy_error`` (the
    response the endpoint gave before it read through the store), never hiding the others.
    Mutation: let ``capital_reader._policy`` skip ``check_pointer`` -- both broken heads read
    a policy and this fails."""
    from sqlalchemy import text

    app, factory, account = await _app(migrated_db, monkeypatch)
    await _seed_policies(factory, account)
    async with factory.begin() as session:
        # Corrupt pointers, behind the triggers: fUST's head off its own revision number,
        # and a fEUR head at fUSD's revision.
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        await session.execute(text(
            "UPDATE capital_policy_heads SET revision = 2 WHERE exchange_account_id = :a "
            "AND symbol = 'fUST'"), {"a": account})
        await session.execute(text(
            "INSERT INTO capital_policy_heads(exchange_account_id, deployment_environment, symbol, "
            "revision_id, revision) SELECT exchange_account_id, deployment_environment, 'fEUR', "
            "revision_id, revision FROM capital_policy_heads WHERE exchange_account_id = :a "
            "AND symbol = 'fUSD'"), {"a": account})
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get(base, headers=AUTH)
    assert response.status_code == 200
    unreadable = {"policy_error": "inconsistent_policy_pointer", "enabled": None,
                  "max_offer_amount": None, "envelope": None, "requests": []}
    assert response.json()["data"]["currencies"] == [
        {"symbol": "fEUR", "revision": 1, **unreadable},
        {"symbol": "fUSD", "revision": 1, "policy_error": None, "enabled": False,
         "max_offer_amount": None, "envelope": None, "requests": []},
        {"symbol": "fUST", "revision": 2, **unreadable},
    ]


@pytest.mark.asyncio
async def test_a_currency_toggle_only_queues_a_request(migrated_db, monkeypatch):
    from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
    from bfx_funding_bot.modules.ledger.tables import CapitalPolicyHeadRow
    app, factory, account = await _app(migrated_db, monkeypatch)
    await _seed_policies(factory, account)
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        disable = await client.post(f"{base}/currencies/fUST/disable", json={"reason": "maintenance"},
                                    headers=AUTH)
        assert disable.status_code == 202
        body = disable.json()["data"]
        assert (body["symbol"], body["action"], body["state"]) == ("fUST", "disable", "requested")
        again = await client.post(f"{base}/currencies/fUST/disable", json={"reason": "x"}, headers=AUTH)
        assert (again.status_code, again.json()["detail"]) == (409, "request_pending")
        # Its own slot: a pending disable never blocks the opposite ask...
        assert (await client.post(f"{base}/currencies/fUST/enable", json={"reason": "y"},
                                  headers=AUTH)).status_code == 202
        # ...and no toggle ever blocks a kill.
        assert (await client.post(f"{base}/kill", json={"reason": "z"}, headers=AUTH)).status_code == 202
        status = await client.get(f"{base}/currency-requests/{body['request_id']}", headers=AUTH)
        assert status.json()["data"]["state"] == "requested"
        assert (await client.get(f"{base}/currency-requests/{uuid4()}", headers=AUTH)).status_code == 404
        overview = (await client.get(base, headers=AUTH)).json()["data"]
        fust = next(c for c in overview["currencies"] if c["symbol"] == "fUST")
        assert sorted((r["action"], r["state"], r["requested_by"]) for r in fust["requests"]) == [
            ("disable", "requested", "operator"), ("enable", "requested", "operator")]
        assert fust["enabled"] is True  # nothing applied yet
    async with factory() as session:
        rows = (await session.scalars(select(CapitalPolicyRequestRow))).all()
        assert sorted(r.action for r in rows) == ["disable", "enable"]
        head = await session.get(CapitalPolicyHeadRow, (account, "ci", "fUST"))
        assert head.revision == 1  # the web API wrote no policy


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body", "status"), [
    ("currencies/fBTC/disable", {"reason": "x"}, 404),       # no applied policy
    ("currencies/fust/disable", {"reason": "x"}, 422),       # not a funding symbol
    ("currencies/fUST/pause", {"reason": "x"}, 422),
    ("currencies/fUST/disable", {"reason": ""}, 422),        # a reason is required
    ("currencies/fUST/disable", {"reason": "  "}, 422),      # blank, not request_pending
    ("currencies/fUST/disable", {"reason": "x", "state": "applied"}, 422),
])
async def test_malformed_currency_requests_are_refused(migrated_db, monkeypatch, path, body, status):
    from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
    app, factory, account = await _app(migrated_db, monkeypatch)
    await _seed_policies(factory, account)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/v1/exchange-accounts/{account}/trading-control/{path}",
                                     json=body, headers=AUTH)
    assert response.status_code == status
    async with factory() as session:
        assert (await session.scalars(select(CapitalPolicyRequestRow))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "principal"), [
    ("viewer", None),
    ("owner", Principal("operator", None, "user")),
])
async def test_only_the_operator_can_toggle_a_currency(migrated_db, monkeypatch, role, principal):
    app, factory, account = await _app(migrated_db, monkeypatch, role=role, principal=principal)
    await _seed_policies(factory, account)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/exchange-accounts/{account}/trading-control/currencies/fUST/disable",
            json={"reason": "x"}, headers=AUTH)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_reads_list_newest_first_and_name_no_effect(migrated_db, monkeypatch):
    """Requests created in the same millisecond list by request id, newest first. A request
    names no effect: its effects name it (ADR 2026-10-08 D9)."""
    from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
    from bfx_funding_bot.modules.execution.operator_requests import insert_request
    app, factory, account = await _app(migrated_db, monkeypatch)
    await _seed_policies(factory, account)
    low, high = sorted([uuid4(), uuid4()])
    common = {"exchange_account_id": account, "deployment_environment": "ci", "reason": "r",
              "requested_by": "operator", "created_at_ms": 7}
    async with factory.begin() as session:
        for request_id, action in ((low, "resume"), (high, "kill")):
            assert await insert_request(session, TradingControlRequestRow,
                                        {**common, "request_id": request_id, "action": action})
        for request_id, action in ((low, "enable"), (high, "disable")):
            assert await insert_request(session, CapitalPolicyRequestRow, {
                **common, "request_id": request_id, "action": action, "symbol": "fUST"})
    base = f"/api/v1/exchange-accounts/{account}/trading-control"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        overview = (await client.get(base, headers=AUTH)).json()["data"]
        single = (await client.get(f"{base}/requests/{low}", headers=AUTH)).json()["data"]
        currency = (await client.get(f"{base}/currency-requests/{low}", headers=AUTH)).json()["data"]
    fust = next(c for c in overview["currencies"] if c["symbol"] == "fUST")
    for listed in (overview["requests"], fust["requests"]):
        assert [r["request_id"] for r in listed] == [str(high), str(low)]
    for shown in (single, currency, *overview["requests"], *fust["requests"]):
        assert not {"trading_state_id", "policy_revision_id"} & set(shown)
