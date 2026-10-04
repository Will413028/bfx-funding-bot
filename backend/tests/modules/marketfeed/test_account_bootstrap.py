from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.accounts.tables import (
    AccountConfigDraft,
    ExchangeAccount,
    ExchangeAccountCredential,
)
from bfx_funding_bot.modules.execution.protocols import Credentials
from bfx_funding_bot.modules.marketfeed.daemon import (
    AccountBootstrap,
    _require_env,
    load_account_bootstrap,
)

_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_KEK = bytes(range(32))
_KEK_B64 = base64.b64encode(_KEK).decode()


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_async_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'bootstrap.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_account(
    factory: async_sessionmaker[AsyncSession],
    *,
    lifecycle_status: str = "active",
    credential: bool = True,
    draft: bool = True,
) -> None:
    async with factory.begin() as session:
        session.add(
            ExchangeAccount(
                id=_ACCOUNT_ID,
                venue="bitfinex",
                label="primary",
                lifecycle_status=lifecycle_status,
            )
        )
        if credential:
            envelope = encrypt_secret_with_aad(
                "account-secret", aad=str(_ACCOUNT_ID), kek=_KEK
            )
            session.add(
                ExchangeAccountCredential(
                    exchange_account_id=_ACCOUNT_ID,
                    venue="bitfinex",
                    label="primary",
                    api_key="account-key",
                    secret_ciphertext=envelope.secret_ciphertext,
                    secret_nonce=envelope.secret_nonce,
                    wrapped_dek=envelope.wrapped_dek,
                    dek_nonce=envelope.dek_nonce,
                    key_version=envelope.key_version,
                    lifecycle_status="active",
                    verified_at=datetime.now(UTC),
                )
            )
        if draft:
            session.add(
                AccountConfigDraft(
                    exchange_account_id=_ACCOUNT_ID,
                    config={"currency": "fUSD"},
                    revision=3,
                    source="operator",
                )
            )


def test_exchange_account_env_is_required_and_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_EXCHANGE_ACCOUNT_ID", raising=False)
    with pytest.raises(ConfigurationError, match="BFX_EXCHANGE_ACCOUNT_ID env var required"):
        _require_env("BFX_EXCHANGE_ACCOUNT_ID")

    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", "not-a-uuid")
    with pytest.raises(ConfigurationError, match="valid UUID"):
        _require_env("BFX_EXCHANGE_ACCOUNT_ID")

    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(_ACCOUNT_ID).upper())
    assert _require_env("BFX_EXCHANGE_ACCOUNT_ID") == str(_ACCOUNT_ID)


def test_live_boot_rejects_legacy_account_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(_ACCOUNT_ID))
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    with pytest.raises(ConfigurationError, match="BFX_ACCOUNT_ID is no longer supported"):
        AccountBootstrap.reject_legacy_realm(phase=Phase.LIVE)


@pytest.mark.asyncio
async def test_bootstrap_loads_account_credential_and_draft(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_account(session_factory)
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(_ACCOUNT_ID))
    monkeypatch.setenv("BFX_VAULT_KEK", _KEK_B64)

    async with session_factory() as session:
        bootstrap = await load_account_bootstrap(
            session,
            deployment_environment="ci",
            allocation_cap_usdt=Decimal("500"),
        )

    assert isinstance(bootstrap, AccountBootstrap)
    assert bootstrap.exchange_account_id == _ACCOUNT_ID
    assert bootstrap.account_id == str(_ACCOUNT_ID)
    assert bootstrap.config_revision == 3
    assert bootstrap.config_draft == {"currency": "fUSD"}
    assert bootstrap.to_context(Credentials("k", "s")).account_id == str(_ACCOUNT_ID)
    assert not hasattr(bootstrap, "credentials")  # the venue wiring owns them


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lifecycle_status", "credential", "message"),
    [
        ("retired", True, "must be active"),
        ("halted", True, "must be active"),
    ],
)
async def test_bootstrap_fails_closed_for_unusable_account(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    lifecycle_status: str,
    credential: bool,
    message: str,
) -> None:
    await _seed_account(
        session_factory, lifecycle_status=lifecycle_status, credential=credential
    )
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(_ACCOUNT_ID))
    monkeypatch.setenv("BFX_VAULT_KEK", _KEK_B64)

    async with session_factory() as session:
        with pytest.raises(ConfigurationError, match=message):
            await load_account_bootstrap(
                session,
                deployment_environment="ci",
                allocation_cap_usdt=Decimal("500"),
            )
