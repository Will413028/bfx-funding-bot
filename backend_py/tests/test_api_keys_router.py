import base64
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # registers APIKey in Base.metadata
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core import auth
from bfx_funding_bot.core.auth import Principal, require_operator
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

    fake = _FakeClient(perms=KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, False)}))

    async def _override_client():
        yield fake

    app.dependency_overrides[require_operator] = _fake_operator
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


def test_verify_funding_only_read_write_verified(app_client):
    # #3 fail-closed: a key with ONLY funding read+write must verify.
    created = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    ).json()["data"]
    app_client._fake._perms = KeyPermissions(scopes={"funding": (True, True)})
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "verified"


def test_verify_other_write_scope_fails_closed(app_client):
    # #3 fail-closed: funding-write ON plus some OTHER (renamed/unknown) write
    # scope must NOT slip through as verified.
    created = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    ).json()["data"]
    app_client._fake._perms = KeyPermissions(scopes={
        "funding": (True, True),
        "orders": (True, True),
        "withdrawals": (False, True),  # renamed dangerous scope
    })
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "failed"
    assert r.json()["data"]["error"].startswith("unexpected_write_scope")


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


def _verify_ok_first(app_client):
    """Create a key and verify it once (-> verified). Returns the key id."""
    created = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    ).json()["data"]
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.json()["data"]["status"] == "verified"
    return created["id"]


@pytest.mark.parametrize("status_code", [429, 503])
def test_verify_transient_does_not_demote(app_client, status_code):
    # #1: rate-limit (429) and server/maintenance (5xx) are TRANSIENT -> 502 and
    # the previously-verified row is left unchanged (not demoted to failed).
    key_id = _verify_ok_first(app_client)
    app_client._fake._error = BitfinexAPIError(
        status_code=status_code, message="transient", raw=None
    )
    r = app_client.post(f"/api/v1/api-keys/{key_id}/verify")
    assert r.status_code == 502
    # status unchanged: list still shows verified
    item = next(i for i in app_client.get("/api/v1/api-keys").json()["data"] if i["id"] == key_id)
    assert item["exchangeStatus"] == "verified"


def test_verify_client_error_demotes(app_client):
    # #1: a genuine 4xx credential error (401) DOES demote a verified row.
    key_id = _verify_ok_first(app_client)
    app_client._fake._error = BitfinexAPIError(status_code=401, message="unauthorized", raw=None)
    r = app_client.post(f"/api/v1/api-keys/{key_id}/verify")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "failed"
    assert r.json()["data"]["error"] == "invalid_credentials"


def test_verify_kek_mismatch_503_not_500(app_client, monkeypatch):
    # #2: a wrong/rotated KEK fails AES-GCM auth on decrypt -> the verify
    # endpoint yields 503 vault_key_mismatch, NOT an unhandled 500.
    created = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    ).json()["data"]
    # Swap the active KEK to a different 32-byte key after the secret was
    # encrypted under the original -> decrypt InvalidTag.
    monkeypatch.setenv("BFX_VAULT_KEK", base64.b64encode(bytes(range(31, -1, -1))).decode())
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 503
    assert r.json()["detail"] == "vault_key_mismatch"


def test_create_encrypt_unaffected_by_kek_mismatch_test(app_client):
    # #2 guard: create/encrypt path stays a normal 201 (sanity that the mismatch
    # only bites on decrypt).
    r = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    )
    assert r.status_code == 201


def test_list_exposes_last_verify_error(app_client):
    # #5: GET /api/v1/api-keys returns lastVerifyError for a failed key.
    created = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    ).json()["data"]
    app_client._fake._perms = KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, True)})
    app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    items = app_client.get("/api/v1/api-keys").json()["data"]
    assert items[0]["exchangeStatus"] == "failed"
    assert items[0]["lastVerifyError"] == "withdraw_must_be_disabled"


def test_create_integrity_error_returns_409(app_client, monkeypatch):
    # #7: a concurrent duplicate INSERT trips the DB unique index ->
    # sqlalchemy IntegrityError -> 409 (not 500).
    from sqlalchemy.exc import IntegrityError

    from bfx_funding_bot.modules.api import api_keys as api_keys_mod

    async def _boom(*args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    monkeypatch.setattr(api_keys_mod.vault, "create_api_key", _boom)
    r = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "key_already_exists"


def test_delete(app_client):
    created = app_client.post("/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}).json()["data"]
    r = app_client.delete(f"/api/v1/api-keys/{created['id']}")
    assert r.status_code == 204
    assert app_client.get("/api/v1/api-keys").json()["data"] == []


def test_non_operator_is_rejected_by_every_api_key_route(app_client):
    """Catches any route wired to require_user instead of require_operator."""
    async def _reject_non_operator():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator_required")

    app_client.app.dependency_overrides[require_operator] = _reject_non_operator
    headers = {"Authorization": "Bearer ignored"}
    requests = (
        ("get", "/api/v1/api-keys", {}),
        ("post", "/api/v1/api-keys", {"json": {"label": "a", "apiKey": "P", "apiSecret": "S"}}),
        ("delete", f"/api/v1/api-keys/{uuid.uuid4()}", {}),
    )

    for method, path, kwargs in requests:
        response = getattr(app_client, method)(path, headers=headers, **kwargs)
        assert response.status_code == 403, path


def test_missing_operator_config_rejects_before_session_access(monkeypatch):
    """Catches a dependency order that opens a database session before authz."""
    monkeypatch.delenv("BFX_OPERATOR_USER_ID", raising=False)
    monkeypatch.setattr(
        auth, "_verify", lambda _: Principal("operator-1", "will@example.com", "admin")
    )
    session_accessed = False

    app = FastAPI()
    app.include_router(build_api_keys_router())

    async def _unexpected_session():
        nonlocal session_accessed
        session_accessed = True
        raise AssertionError("database session must not be opened before operator auth")
        yield

    app.dependency_overrides[get_session] = _unexpected_session
    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/api-keys", headers={"Authorization": "Bearer ignored"}
    )

    assert response.status_code == 503
    assert session_accessed is False


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

    app_client.app.dependency_overrides[require_operator] = _other_user
    r = app_client.delete(f"/api/v1/api-keys/{created['id']}")
    assert r.status_code == 404


def test_verify_another_users_key_404(app_client):
    created = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    ).json()["data"]

    async def _other_user():
        return Principal(user_id="user_xyz", email="other@example.com", role="operator")

    app_client.app.dependency_overrides[require_operator] = _other_user
    r = app_client.post(f"/api/v1/api-keys/{created['id']}/verify")
    assert r.status_code == 404


def test_create_kek_missing_503(app_client, monkeypatch):
    # The app_client fixture sets BFX_VAULT_KEK during setup; remove it so the
    # router's _require_kek() -> load_kek() raises VaultNotConfiguredError -> 503.
    monkeypatch.delenv("BFX_VAULT_KEK", raising=False)
    r = app_client.post(
        "/api/v1/api-keys", json={"label": "a", "apiKey": "P", "apiSecret": "S"}
    )
    assert r.status_code == 503
    assert r.json()["detail"] == "vault_not_configured"
