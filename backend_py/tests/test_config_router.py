import pytest_asyncio
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # side-effect: registers ORM models for Base.metadata.create_all
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401  # side-effect: registers ORM models
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.api.config import build_config_router
from bfx_funding_bot.modules.api.deps import get_session

_VALID = {
    "currency": "USD",
    "amount": {"min": 50, "max": 10000},
    "rate": {"min": 0.0001, "max": 0.001},
    "period": {"min": 2, "max": 30},
    "autoRenew": True,
}


@pytest_asyncio.fixture
async def app_client(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

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
    return TestClient(app)


def test_get_absent_returns_404(app_client):
    assert app_client.get("/api/v1/configs").status_code == 404


def test_put_creates_then_get_returns(app_client):
    r = app_client.put("/api/v1/configs", json=_VALID)
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["userId"] == "user_abc"
    assert body["config"]["currency"] == "USD"
    assert body["id"] and body["createdAt"]

    g = app_client.get("/api/v1/configs")
    assert g.status_code == 200
    assert g.json()["data"]["config"]["autoRenew"] is True


def test_put_twice_updates_in_place(app_client):
    first = app_client.put("/api/v1/configs", json=_VALID).json()["data"]
    second = app_client.put(
        "/api/v1/configs", json={**_VALID, "currency": "UST"}
    ).json()["data"]
    assert second["id"] == first["id"]
    assert second["config"]["currency"] == "UST"


def test_put_invalid_returns_422_or_400(app_client):
    bad = {**_VALID, "amount": {"min": -1, "max": 10}}
    assert app_client.put("/api/v1/configs", json=bad).status_code in (400, 422)


def test_put_min_gt_max_rejected(app_client):
    bad = {**_VALID, "rate": {"min": 0.002, "max": 0.001}}
    assert app_client.put("/api/v1/configs", json=bad).status_code in (400, 422)


def test_delete_existing_then_absent(app_client):
    app_client.put("/api/v1/configs", json=_VALID)
    assert app_client.delete("/api/v1/configs").status_code == 204
    assert app_client.delete("/api/v1/configs").status_code == 404


def test_non_operator_is_rejected_by_every_config_route(app_client):
    """Catches any config route wired to require_user instead of require_operator."""
    async def _reject_non_operator():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator_required")

    app_client.app.dependency_overrides[require_operator] = _reject_non_operator
    headers = {"Authorization": "Bearer ignored"}
    requests = (
        ("get", "/api/v1/configs", {}),
        ("put", "/api/v1/configs", {"json": _VALID}),
        ("delete", "/api/v1/configs", {}),
    )

    for method, path, kwargs in requests:
        response = getattr(app_client, method)(path, headers=headers, **kwargs)
        assert response.status_code == 403, path


def test_requires_auth():
    app = FastAPI()
    app.include_router(build_config_router())
    c = TestClient(app)
    assert c.get("/api/v1/configs").status_code in (401, 403)
    assert c.put("/api/v1/configs", json=_VALID).status_code in (401, 403)
    assert c.delete("/api/v1/configs").status_code in (401, 403)


def test_cross_tenant_isolation(app_client):
    # Tenant-isolation invariant (matches the sibling api_keys suite): a config
    # saved by user_abc must be invisible/undeletable to a different principal,
    # and that principal's 404 path must not touch user_abc's row.
    app_client.put("/api/v1/configs", json=_VALID)  # saved as user_abc

    async def _other_user():
        return Principal(user_id="user_xyz", email="other@example.com", role="operator")

    app_client.app.dependency_overrides[require_operator] = _other_user
    assert app_client.get("/api/v1/configs").status_code == 404
    assert app_client.delete("/api/v1/configs").status_code == 404

    async def _abc_user():
        return Principal(user_id="user_abc", email="will@example.com", role="operator")

    app_client.app.dependency_overrides[require_operator] = _abc_user
    assert app_client.get("/api/v1/configs").status_code == 200  # untouched
