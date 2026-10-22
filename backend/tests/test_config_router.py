import logging
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # side-effect: registers ORM models for Base.metadata.create_all
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401  # side-effect: registers ORM models
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    grant_membership,
    upsert_account_config_draft,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.api.config import build_config_router
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from tests.ingress_support import attach_ingress

_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_CONFIG_PATH = f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/config-draft"

_VALID = {
    "currency": "USD",
    "amount": {"min": 50, "max": 10000},
    "rate": {"min": 0.0001, "max": 0.001},
    "period": {"min": 2, "max": 30},
    "autoRenew": True,
}


@pytest_asyncio.fixture
async def app_client(sqlite_engine, monkeypatch):
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as seed:
        seed.add(
            ExchangeAccount(
                id=_ACCOUNT_ID,
                venue="bitfinex",
                label="Primary",
                lifecycle_status="active",
            )
        )
        await seed.flush()
        await grant_membership(
            seed,
            exchange_account_id=_ACCOUNT_ID,
            user_id="user_abc",
            role="owner",
        )
        await seed.commit()

    app = FastAPI()
    app.include_router(build_config_router())

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
    app.state.session_factory = factory
    return TestClient(app)


def test_get_absent_returns_404(app_client):
    assert app_client.get(_CONFIG_PATH).status_code == 404


def test_put_creates_then_get_returns(app_client):
    r = app_client.put(_CONFIG_PATH, json=_VALID)
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["exchangeAccountId"] == str(_ACCOUNT_ID)
    assert body["revision"] == 1
    assert body["source"] == "operator"
    assert body["config"]["currency"] == "USD"
    assert body["id"] and body["createdAt"]

    g = app_client.get(_CONFIG_PATH)
    assert g.status_code == 200
    assert g.json()["data"]["config"]["autoRenew"] is True


def test_put_twice_updates_in_place(app_client):
    first = app_client.put(_CONFIG_PATH, json=_VALID).json()["data"]
    second = app_client.put(
        _CONFIG_PATH, json={**_VALID, "currency": "UST"}
    ).json()["data"]
    assert second["id"] == first["id"]
    assert second["config"]["currency"] == "UST"


async def _store_draft(app_client, config):
    """A draft written as an earlier schema left it: the domain writer stores any mapping."""
    async with app_client.app.state.session_factory() as session:
        await upsert_account_config_draft(
            session, exchange_account_id=_ACCOUNT_ID, config=config, source="operator")
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [
    pytest.param({k: v for k, v in _VALID.items() if k != "autoRenew"}, id="missing-field"),
    pytest.param({**_VALID, "rate": {"min": 0.002, "max": 0.0001}}, id="invalid-value"),
    pytest.param({**_VALID, "maxOffers": 4}, id="key-no-longer-in-schema"),
])
async def test_a_draft_stored_under_another_schema_is_listed_unreadable_and_overwritable(
        app_client, caplog, stored):
    """GET never fails on a stored draft: an operator must still see that it exists and be
    able to overwrite it. The log names the fields, never the stored values."""
    await _store_draft(app_client, stored)
    caplog.set_level(logging.ERROR, logger="bfx_funding_bot.modules.api.config")

    response = app_client.get(_CONFIG_PATH)

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["config"] is None
    assert body["configError"] == "config_unreadable"
    assert body["revision"] == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "config_draft_unreadable" in logged
    assert "0.0001" not in logged and "10000" not in logged

    overwritten = app_client.put(_CONFIG_PATH, json=_VALID)
    assert overwritten.status_code == 200, overwritten.text
    readable = app_client.get(_CONFIG_PATH).json()["data"]
    assert readable["revision"] == 2
    assert readable["configError"] is None
    assert readable["config"] == {**_VALID, "amount": {"min": 50.0, "max": 10000.0},
                                  "period": {"min": 2.0, "max": 30.0}}


def test_put_invalid_returns_422_or_400(app_client):
    bad = {**_VALID, "amount": {"min": -1, "max": 10}}
    assert app_client.put(_CONFIG_PATH, json=bad).status_code in (400, 422)


def test_put_min_gt_max_rejected(app_client):
    bad = {**_VALID, "rate": {"min": 0.002, "max": 0.001}}
    assert app_client.put(_CONFIG_PATH, json=bad).status_code in (400, 422)


def test_delete_existing_then_absent(app_client):
    app_client.put(_CONFIG_PATH, json=_VALID)
    assert app_client.delete(_CONFIG_PATH).status_code == 204
    assert app_client.delete(_CONFIG_PATH).status_code == 404


def test_non_operator_is_rejected_by_every_config_route(app_client):
    """A route not gated by require_operator would answer instead of refusing."""
    async def _reject_non_operator():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator_required")


    async def _skip_rate_limit():
        return None

    app_client.app.dependency_overrides[require_operator] = _reject_non_operator
    app_client.app.dependency_overrides[shared_rate_limit_dependency()] = _skip_rate_limit
    requests = (
        ("get", _CONFIG_PATH, {}),
        ("put", _CONFIG_PATH, {"json": _VALID}),
        ("delete", _CONFIG_PATH, {}),
    )

    for method, path, kwargs in requests:
        response = getattr(app_client, method)(path, **kwargs)
        assert response.status_code == 403, path


def test_requires_auth():
    app = attach_ingress(FastAPI())
    app.include_router(build_config_router())
    c = TestClient(app)
    assert c.get(_CONFIG_PATH).status_code in (401, 403)
    assert c.put(_CONFIG_PATH, json=_VALID).status_code in (401, 403)
    assert c.delete(_CONFIG_PATH).status_code in (401, 403)


def test_cross_tenant_isolation(app_client):
    # Tenant-isolation invariant (matches the sibling api_keys suite): a config
    # saved by user_abc must be invisible/undeletable to a different principal,
    # and that principal's 404 path must not touch user_abc's row.
    app_client.put(_CONFIG_PATH, json=_VALID)  # saved as user_abc

    async def _other_user():
        return Principal(user_id="user_xyz", email="other@example.com", role="operator")

    app_client.app.dependency_overrides[require_operator] = _other_user
    assert app_client.get(_CONFIG_PATH).status_code == 404
    assert app_client.delete(_CONFIG_PATH).status_code == 404

    async def _abc_user():
        return Principal(user_id="user_abc", email="will@example.com", role="operator")

    app_client.app.dependency_overrides[require_operator] = _abc_user
    assert app_client.get(_CONFIG_PATH).status_code == 200  # untouched
