"""The credential router: a read-only list since 9c-2b (writes go through the operator CLI;
ADR 2026-10-15 D4). The vault's verify verdicts are ``tests/modules/accounts/
test_account_credential_verify.py``."""
import base64
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # registers APIKey in Base.metadata
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.modules.accounts import vault
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from tests.ingress_support import attach_ingress, operator_headers

_KEK = bytes(range(32))
_KEK_B64 = base64.b64encode(_KEK).decode()
_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_KEYS_PATH = f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/credentials"


class _FakeClient:
    def __init__(self, scopes):
        self._scopes = scopes

    async def get_key_permissions(self, *, ctx):
        return KeyPermissions(scopes=self._scopes)


@pytest_asyncio.fixture
async def app_client(sqlite_engine, monkeypatch):
    monkeypatch.setenv("BFX_VAULT_KEK", _KEK_B64)
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

    app.dependency_overrides[require_operator] = _fake_operator
    app.dependency_overrides[get_session] = _override_session
    client = TestClient(app)
    client._factory = factory  # let tests seed credentials as the operator CLI would
    return client


async def _seed(client, *, scopes=None) -> UUID:
    """A pending credential, verified with ``scopes`` when given (the bootstrap's path)."""
    async with client._factory.begin() as session:
        row = await vault.create_account_credential(
            session, exchange_account_id=_ACCOUNT_ID, user_id="user_abc", label="main",
            api_key="PUB", api_secret="SEC", kek=_KEK, bitfinex_user_id=4242)
        if scopes is not None:
            await vault.verify_account_credential(
                session, _FakeClient(scopes), exchange_account_id=_ACCOUNT_ID,
                user_id="user_abc", key_id=row.id, kek=_KEK)
        return row.id


async def _credentials(client) -> list[ExchangeAccountCredential]:
    async with client._factory() as session:
        return list(await session.scalars(select(ExchangeAccountCredential)))


@pytest.mark.asyncio
async def test_list_masks_secret(app_client):
    await _seed(app_client)
    r = app_client.get(_KEYS_PATH)
    assert r.status_code == 200
    items = r.json()["data"]
    assert len(items) == 1
    assert (items[0]["apiKey"], items[0]["apiSecret"], items[0]["status"]) == (
        "PUB", "****", "pending")


@pytest.mark.asyncio
async def test_list_exposes_last_verify_error(app_client):
    await _seed(app_client, scopes={"funding": (True, True), "withdraw": (False, True)})
    items = app_client.get(_KEYS_PATH).json()["data"]
    assert items[0]["status"] == "pending"
    assert items[0]["lastVerifyError"] == "withdraw_must_be_disabled"


@pytest.mark.asyncio
async def test_list_shows_a_verified_credential(app_client):
    await _seed(app_client, scopes={"funding": (True, True)})
    assert app_client.get(_KEYS_PATH).json()["data"][0]["status"] == "verified"


@pytest.mark.asyncio
async def test_credential_writes_are_not_routes(app_client):
    """Create, verify and delete went with 9c-2b: the operator CLI writes credentials, and
    the database refuses the web API's role any INSERT or UPDATE on them."""
    key_id = await _seed(app_client, scopes={"funding": (True, True)})
    before = [(c.id, c.lifecycle_status, c.verified_at, c.last_verify_error)
              for c in await _credentials(app_client)]
    for method, path, kwargs in (
            ("post", _KEYS_PATH, {"json": {"label": "a", "apiKey": "P", "apiSecret": "S"}}),
            ("post", f"{_KEYS_PATH}/{key_id}/verify", {}),
            ("delete", f"{_KEYS_PATH}/{key_id}", {})):
        response = getattr(app_client, method)(path, **kwargs)
        assert response.status_code in (404, 405), (method, path, response.status_code)
    after = [(c.id, c.lifecycle_status, c.verified_at, c.last_verify_error)
             for c in await _credentials(app_client)]
    assert after == before


def test_non_operator_is_rejected_by_every_api_key_route(app_client):
    """A route not gated by require_operator would answer instead of refusing."""
    async def _reject_non_operator():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator_required")


    async def _skip_rate_limit():
        return None

    app_client.app.dependency_overrides[require_operator] = _reject_non_operator
    app_client.app.dependency_overrides[shared_rate_limit_dependency()] = _skip_rate_limit
    assert app_client.get(_KEYS_PATH).status_code == 403


def test_missing_operator_config_rejects_before_session_access():
    """Catches a dependency order that opens a database session before authz: an app
    without the lifespan's ingress settings refuses (503) before any session."""
    session_accessed = False

    app = FastAPI()
    app.include_router(build_api_keys_router())

    async def _unexpected_session():
        nonlocal session_accessed
        session_accessed = True
        raise AssertionError("database session must not be opened before operator auth")
        yield

    app.dependency_overrides[get_session] = _unexpected_session
    response = TestClient(app, raise_server_exceptions=False).get(_KEYS_PATH, headers=operator_headers())

    assert response.status_code == 503
    assert session_accessed is False


def test_requires_auth():
    app = attach_ingress(FastAPI())
    app.include_router(build_api_keys_router())
    c = TestClient(app)
    assert c.get(_KEYS_PATH).status_code in (401, 403)
