import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bfx_funding_bot.modules.api.routers import build_router


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    app.include_router(build_router())

    from bfx_funding_bot.core.auth import Principal, require_user

    async def _fake_user():
        return Principal(user_id="user_abc", email="will@example.com", role="admin")

    app.dependency_overrides[require_user] = _fake_user
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
