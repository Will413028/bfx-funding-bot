"""``apps/venue.py``: what each venue's wiring carries.

Mutations (one at a time; revert after each): the simulated credentials open the vault
(``test_simulated_credentials_*``); a simulated boot without a ``VenueSeam`` goes on to open
the venue (``test_a_simulated_boot_without_a_seam_*``).
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.apps import venue as venue_module
from bfx_funding_bot.apps.venue import (
    BITFINEX_CAPABILITIES,
    SIMULATED_CAPABILITIES,
    _throwaway_credentials,
    _vault_credentials,
)
from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
KEK_B64 = base64.b64encode(bytes(range(32))).decode()


def test_the_capabilities_each_wiring_states() -> None:
    assert BITFINEX_CAPABILITIES.auth_ws == "required"
    assert SIMULATED_CAPABILITIES.auth_ws == "forbidden"


def test_simulated_credentials_are_generated_per_call_and_never_touch_the_vault(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_VAULT_KEK", raising=False)

    def closed(*args: object, **kwargs: object) -> object:
        raise AssertionError("a simulated boot must not open the vault")

    monkeypatch.setattr(venue_module, "load_kek", closed)
    monkeypatch.setattr(venue_module, "load_account_credentials", closed)
    first, second = _throwaway_credentials(), _throwaway_credentials()
    assert first.api_key and first.api_secret and first != second


def test_simulated_credentials_refuse_a_vault_key_in_the_environment(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_VAULT_KEK", KEK_B64)
    with pytest.raises(ConfigurationError, match="BFX_VAULT_KEK must not be set"):
        _throwaway_credentials()


@pytest.fixture
async def factory(tmp_path: Path):
    engine = make_async_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'venue.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(factory: Any, *, credential: str | None = "active") -> None:
    envelope = encrypt_secret_with_aad("account-secret", aad=str(ACCOUNT), kek=bytes(range(32)))
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="primary"))
        if credential is not None:
            session.add(ExchangeAccountCredential(
                exchange_account_id=ACCOUNT, venue="bitfinex", label="primary",
                api_key="account-key", secret_ciphertext=envelope.secret_ciphertext,
                secret_nonce=envelope.secret_nonce, wrapped_dek=envelope.wrapped_dek,
                dek_nonce=envelope.dek_nonce, key_version=envelope.key_version,
                lifecycle_status=credential, verified_at=datetime.now(UTC),
                bitfinex_user_id=4242))


async def test_the_bitfinex_wiring_reads_the_vault(factory, monkeypatch) -> None:
    await _seed(factory)
    monkeypatch.setenv("BFX_VAULT_KEK", KEK_B64)
    async with factory() as session:
        credentials = await _vault_credentials(session, ACCOUNT)
    assert (credentials.api_key, credentials.api_secret) == ("account-key", "account-secret")


@pytest.mark.parametrize(("credential", "kek", "message"), [
    (None, True, "active Bitfinex credential"),
    ("retired", True, "active Bitfinex credential"),
    ("active", False, "BFX_VAULT_KEK is required"),
])
async def test_the_bitfinex_wiring_fails_closed_without_a_usable_credential(
        factory, monkeypatch, credential, kek, message) -> None:
    await _seed(factory, credential=credential)
    if kek:
        monkeypatch.setenv("BFX_VAULT_KEK", KEK_B64)
    else:
        monkeypatch.delenv("BFX_VAULT_KEK", raising=False)
    async with factory() as session:
        with pytest.raises(ConfigurationError, match=message):
            await _vault_credentials(session, ACCOUNT)
