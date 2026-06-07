import base64
import uuid

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # registers APIKey in Base.metadata
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router
from bfx_funding_bot.modules.api.deps import get_bitfinex_auth_rest, get_session

_KEK_B64 = base64.b64encode(bytes(range(32))).decode()


class _FakeClient:
    def __init__(self, perms=None, error=None):
        self._perms = perms
        self._error = error

    async def get_key_permissions(self, *, ctx):
        if self._error:
            raise self._error
        return self._perms


@pytest_asyncio.fixture
async def app_client(sqlite_engine, monkeypatch):
    monkeypatch.setenv("BFX_VAULT_KEK", _KEK_B64)
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(build_api_keys_router())

    async def _fake_user():
        return Principal(user_id="user_abc", email="will@example.com", role="operator")

    async def _override_session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    fake = _FakeClient(perms=KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, False)}))

    async def _override_client():
        yield fake

    app.dependency_overrides[require_user] = _fake_user
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_bitfinex_auth_rest] = _override_client
    client = TestClient(app)
    client._fake = fake  # let tests mutate perms/error
    return client


def test_create_then_list_masks_secret(app_client):
    r = app_client.post("/api/v1/api-keys", json={"label": "main", "apiKey": "PUB", "apiSecret": "SEC"})
    assert r.status_code == 201, r.text
    body = r.json()["data"]
    assert body["apiKey"] == "PUB"
    assert body["apiSecret"] == "****"
    assert body["exchangeStatus"] == "unverified"

    r2 = app_client.get("/api/v1/api-keys")
    assert r2.status_code == 200
    items = r2.json()["data"]
    assert len(items) == 1
    assert items[0]["apiSecret"] == "****"


def test_duplicate_returns_409(app_client):
    app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"})
    r = app_client.post("/api/v1/api-keys", json={"label": "b", "apiKey": "P2", "apiSecret": "S2"})
    assert r.status_code == 409


def test_verify_marks_verified(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "verified"


def test_verify_withdraw_enabled_fails(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    app_client._fake._perms = KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, True)})
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "failed"
    assert r.json()["data"]["error"] == "withdraw_must_be_disabled"


def test_verify_unknown_id_404(app_client):
    r = app_client.post(f"/api/v1/api-keys/{uuid.uuid4()}/verify")
    assert r.status_code == 404


def test_verify_transport_error_502(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    app_client._fake._error = BitfinexAPIError(status_code=0, message="transport", raw=None)
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 502


def test_verify_shape_error_502(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    app_client._fake._error = BitfinexShapeError("malformed permissions response")
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 502


def test_delete(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    r = app_client.delete(f"/api/v1/api-keys/{created['id']}")
    assert r.status_code == 204
    assert app_client.get("/api/v1/api-keys").json()["data"] == []


def test_requires_auth():
    app = FastAPI()
    app.include_router(build_api_keys_router())
    c = TestClient(app)
    assert c.get("/api/v1/api-keys").status_code in (401, 403)
    assert c.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).status_code in (401, 403)
    assert c.delete(f"/api/v1/api-keys/{uuid.uuid4()}").status_code in (401, 403)


def test_delete_another_users_key_404(app_client):
    created = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    ).json()["data"]

    async def _other_user():
        return Principal(user_id="user_xyz", email="other@example.com", role="operator")

    app_client.app.dependency_overrides[require_user] = _other_user
    r = app_client.delete(f"/api/v1/api-keys/{created['id']}")
    assert r.status_code == 404


def test_verify_another_users_key_404(app_client):
    created = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    ).json()["data"]

    async def _other_user():
        return Principal(user_id="user_xyz", email="other@example.com", role="operator")

    app_client.app.dependency_overrides[require_user] = _other_user
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 404
