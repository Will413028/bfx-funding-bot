import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bfx_funding_bot.core.auth import Principal, require_operator, require_user
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.routers import build_router


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    app.include_router(build_router())

    async def _fake_user():
        return Principal(user_id="user_abc", email="will@example.com", role="admin")

    app.dependency_overrides[require_operator] = _fake_user
    return TestClient(app)


def test_profile_requires_auth():
    app = FastAPI()
    app.include_router(build_router())
    c = TestClient(app)
    r = c.get("/api/v1/profile")
    assert r.status_code in (401, 403)


def test_profile_returns_principal(client):
    r = client.get("/api/v1/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["userId"] == "user_abc"
    assert body["data"]["email"] == "will@example.com"
    assert body["data"]["role"] == "admin"


def test_profile_rejects_non_operator_after_skipping_router_rate_limit(client):
    """Catches profile regressing from require_operator to require_user."""
    async def _reject_non_operator():
        raise HTTPException(status_code=403, detail="operator_required")

    async def _permissive_user():
        return Principal(user_id="user_abc", email="will@example.com", role="admin")

    async def _skip_rate_limit():
        return None

    client.app.dependency_overrides[require_operator] = _reject_non_operator
    client.app.dependency_overrides[require_user] = _permissive_user
    client.app.dependency_overrides[shared_rate_limit_dependency()] = _skip_rate_limit

    assert client.get("/api/v1/profile").status_code == 403
