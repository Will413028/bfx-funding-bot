from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.auth import Principal
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    require_account_member,
)
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router
from bfx_funding_bot.modules.api.attribution import build_attribution_router
from bfx_funding_bot.modules.api.config import build_config_router
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.projections import build_projections_router
from bfx_funding_bot.modules.api.routers import build_router
from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow

_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


@pytest_asyncio.fixture
async def session(sqlite_engine) -> AsyncSession:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as value:
        yield value


async def _seed_account(
    session: AsyncSession,
    *,
    lifecycle_status: str = "active",
    user_id: str = "operator-1",
    role: str = "owner",
) -> None:
    session.add(
        ExchangeAccount(
            id=_ACCOUNT_ID,
            venue="bitfinex",
            label="Primary",
            lifecycle_status=lifecycle_status,
        )
    )
    await session.flush()
    await grant_membership(
        session,
        exchange_account_id=_ACCOUNT_ID,
        user_id=user_id,
        role=role,
    )


@pytest.mark.asyncio
async def test_member_context_is_explicit_and_role_scoped(session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "canary")
    await _seed_account(session, role="viewer")

    context = await require_account_member(
        exchange_account_id=_ACCOUNT_ID,
        user=Principal(user_id="operator-1", email="operator@example.com", role="admin"),
        session=session,
    )

    assert isinstance(context, ExchangeAccountContext)
    assert context.exchange_account_id == _ACCOUNT_ID
    assert context.user_id == "operator-1"
    assert context.role == "viewer"
    assert context.can_write is False
    assert context.deployment_environment == "canary"


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["missing-user", "operator-2"])
async def test_missing_membership_is_non_enumerating_404(
    session: AsyncSession, user_id: str
) -> None:
    await _seed_account(session)

    with pytest.raises(HTTPException) as error:
        await require_account_member(
            exchange_account_id=_ACCOUNT_ID,
            user=Principal(user_id=user_id, email=None, role="admin"),
            session=session,
        )

    assert error.value.status_code == 404
    assert error.value.detail == "not_found"


@pytest.mark.asyncio
async def test_unknown_and_retired_accounts_are_non_enumerating_404(
    session: AsyncSession,
) -> None:
    with pytest.raises(HTTPException) as unknown:
        await require_account_member(
            exchange_account_id=_ACCOUNT_ID,
            user=Principal(user_id="operator-1", email=None, role="admin"),
            session=session,
        )
    assert unknown.value.status_code == 404
    assert unknown.value.detail == "not_found"

    await _seed_account(session, lifecycle_status="retired")
    with pytest.raises(HTTPException) as retired:
        await require_account_member(
            exchange_account_id=_ACCOUNT_ID,
            user=Principal(user_id="operator-1", email=None, role="admin"),
            session=session,
        )
    assert retired.value.status_code == 404
    assert retired.value.detail == "not_found"


def test_implicit_default_account_is_not_a_valid_path() -> None:
    from bfx_funding_bot.modules.accounts.exchange_accounts import account_id_canonical

    with pytest.raises(ValueError, match="UUID"):
        account_id_canonical("default")


@pytest_asyncio.fixture
async def scoped_app(sqlite_engine, monkeypatch) -> TestClient:
    monkeypatch.setenv("BFX_VAULT_KEK", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as seed:
        await _seed_account(seed)
        await grant_membership(
            seed,
            exchange_account_id=_ACCOUNT_ID,
            user_id="viewer-1",
            role="viewer",
        )
        other_id = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        seed.add(
            ExchangeAccount(
                id=other_id,
                venue="bitfinex",
                label="Other",
                lifecycle_status="active",
            )
        )
        await seed.flush()
        await grant_membership(
            seed,
            exchange_account_id=other_id,
            user_id="other-operator",
            role="owner",
        )
        seed.add(
            PositionStateRow(
                account_id="legacy-a",
                exchange_account_id=_ACCOUNT_ID,
                deployment_environment="prod",
                symbol="fUST",
                reserved=Decimal("0"),
                realized=Decimal("2"),
                last_updated_ms=1,
                last_event_seq=1,
            )
        )
        seed.add(
            PositionStateRow(
                account_id="legacy-b",
                exchange_account_id=other_id,
                deployment_environment="prod",
                symbol="fUSD",
                reserved=Decimal("0"),
                realized=Decimal("9"),
                last_updated_ms=1,
                last_event_seq=1,
            )
        )
        await seed.commit()

    app = FastAPI()
    app.include_router(build_router())
    app.include_router(build_api_keys_router())
    app.include_router(build_config_router())
    app.include_router(build_projections_router())
    app.include_router(build_attribution_router())
    current_user = {"value": "operator-1"}

    async def _operator() -> Principal:
        return Principal(
            user_id=current_user["value"], email="operator@example.com", role="admin"
        )

    async def _session():
        async with factory() as value:
            try:
                yield value
                await value.commit()
            except Exception:
                await value.rollback()
                raise

    from bfx_funding_bot.core.auth import require_operator

    app.dependency_overrides[require_operator] = _operator
    app.dependency_overrides[get_session] = _session
    client = TestClient(app)
    client.current_user = current_user  # type: ignore[attr-defined]
    return client


def test_bootstrap_and_projection_are_account_scoped(scoped_app: TestClient) -> None:
    accounts = scoped_app.get("/api/v1/exchange-accounts")
    assert accounts.status_code == 200
    assert [item["exchangeAccountId"] for item in accounts.json()["data"]] == [str(_ACCOUNT_ID)]

    positions = scoped_app.get(f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/positions")
    assert positions.status_code == 200
    assert positions.json()["data"][0]["symbol"] == "fUST"
    assert positions.json()["data"][0]["realized"] == "2"


def test_nonmember_account_path_is_404(scoped_app: TestClient) -> None:
    other_id = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    response = scoped_app.get(f"/api/v1/exchange-accounts/{other_id}/positions")
    assert response.status_code == 404
    assert response.json()["detail"] == "not_found"


def test_viewer_can_read_but_cannot_mutate(scoped_app: TestClient) -> None:
    scoped_app.current_user["value"] = "viewer-1"
    assert scoped_app.get(f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/positions").status_code == 200
    payload = {
        "label": "main",
        "apiKey": "PUB",
        "apiSecret": "SEC",
    }
    assert scoped_app.post(
        f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/credentials", json=payload
    ).status_code == 403
    config = {
        "currency": "USD",
        "amount": {"min": 50, "max": 100},
        "rate": {"min": 0.0001, "max": 0.001},
        "period": {"min": 2, "max": 30},
        "autoRenew": True,
    }
    assert scoped_app.put(
        f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/config-draft", json=config
    ).status_code == 403


def test_owner_can_manage_account_credential_and_config(scoped_app: TestClient) -> None:
    payload = {"label": "main", "apiKey": "PUB", "apiSecret": "SEC"}
    created = scoped_app.post(
        f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/credentials", json=payload
    )
    assert created.status_code == 201, created.text
    credential = created.json()["data"]
    assert credential["exchangeAccountId"] == str(_ACCOUNT_ID)
    assert credential["apiSecret"] == "****"
    assert credential["status"] == "unverified"

    listed = scoped_app.get(f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/credentials")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [credential["id"]]

    config = {
        "currency": "USD",
        "amount": {"min": 50, "max": 100},
        "rate": {"min": 0.0001, "max": 0.001},
        "period": {"min": 2, "max": 30},
        "autoRenew": True,
    }
    saved = scoped_app.put(
        f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/config-draft", json=config
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["revision"] == 1

    fetched = scoped_app.get(f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/config-draft")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["config"] == config

    deleted = scoped_app.delete(
        f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/credentials/{credential['id']}"
    )
    assert deleted.status_code == 204
    # Credential lifecycle is retained for audit rather than hard-deleted.
    assert scoped_app.get(
        f"/api/v1/exchange-accounts/{_ACCOUNT_ID}/credentials"
    ).json()["data"][0]["status"] == "retired"


def test_retired_account_is_hidden_from_command_routes(scoped_app: TestClient) -> None:
    # The dependency-level suite covers a persisted retired row; this route
    # check covers the command path's identical non-enumerating boundary for a
    # UUID that is not present in the account registry.
    missing = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
    response = scoped_app.post(
        f"/api/v1/exchange-accounts/{missing}/credentials",
        json={"label": "main", "apiKey": "PUB", "apiSecret": "SEC"},
    )
    assert response.status_code == 404


def test_unscoped_private_routes_are_not_registered(scoped_app: TestClient) -> None:
    for path in (
        "/api/v1/api-keys",
        "/api/v1/configs",
        "/api/v1/positions",
        "/api/v1/offers",
        "/api/v1/executions",
        "/api/v1/attribution/weekly",
    ):
        assert scoped_app.get(path).status_code == 404, path
