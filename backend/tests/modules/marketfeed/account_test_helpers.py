"""Reusable identity rows for daemon SQLite wiring tests."""
from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
from bfx_funding_bot.modules.accounts.tables import (
    ExchangeAccount,
    ExchangeAccountCredential,
)

TEST_EXCHANGE_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_VAULT_KEK = bytes(range(32))
TEST_VAULT_KEK_B64 = base64.b64encode(TEST_VAULT_KEK).decode()


async def stamp_schema_head(engine: AsyncEngine) -> None:
    """Record this build's schema head, as ``alembic upgrade`` would on Postgres."""
    from sqlalchemy import text

    from bfx_funding_bot.core.schema_head import build_head
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
                           {"head": build_head()})


def configure_account_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the explicit identity and test vault expected by build_daemon."""
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(TEST_EXCHANGE_ACCOUNT_ID))
    monkeypatch.setenv("BFX_VAULT_KEK", TEST_VAULT_KEK_B64)


def configure_live_wiring_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Normal live construction: explicit account, live phase, projector version."""
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_PHASE", "live")
    monkeypatch.delenv("BFX_ALLOCATION_CAP_USDT", raising=False)
    monkeypatch.setenv("BFX_PROJECTOR_VERSION", "execution-state-v1")


async def seed_exchange_account(engine: AsyncEngine, *, capital_policies: bool = True) -> None:
    """Provision synthetic identity and explicit applied policies, never an env fallback."""
    envelope = encrypt_secret_with_aad(
        "test_secret", aad=str(TEST_EXCHANGE_ACCOUNT_ID), kek=TEST_VAULT_KEK
    )
    await stamp_schema_head(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(
            ExchangeAccount(
                id=TEST_EXCHANGE_ACCOUNT_ID,
                venue="bitfinex",
                label="test-account",
                lifecycle_status="active",
            )
        )
        session.add(
            ExchangeAccountCredential(
                exchange_account_id=TEST_EXCHANGE_ACCOUNT_ID,
                venue="bitfinex",
                label="test-credential",
                api_key="test_key",
                secret_ciphertext=envelope.secret_ciphertext,
                secret_nonce=envelope.secret_nonce,
                wrapped_dek=envelope.wrapped_dek,
                dek_nonce=envelope.dek_nonce,
                key_version=envelope.key_version,
                lifecycle_status="active",
                verified_at=datetime.now(UTC),
            )
        )
    if capital_policies:
        from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
        from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
        for environment in ("ci", "prod", "shadow"):
            repo = CapitalRepository(account_id=TEST_EXCHANGE_ACCOUNT_ID,
                                     environment=environment, max_snapshot_age_ms=10000)
            async with factory.begin() as session:
                for symbol in ("fUST", "fUSD"):
                    await repo.apply_policy(session, symbol=symbol,
                        policy=CapitalPolicy(enabled=symbol == "fUST"), expected_revision=0,
                        source={"synthetic_fixture": True})


__all__ = [
    "TEST_EXCHANGE_ACCOUNT_ID",
    "TEST_VAULT_KEK_B64",
    "configure_account_env",
    "configure_live_wiring_env",
    "seed_exchange_account",
    "stamp_schema_head",
]
