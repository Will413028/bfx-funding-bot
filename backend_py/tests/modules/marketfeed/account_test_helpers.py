"""Reusable identity rows for daemon SQLite wiring tests."""
from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
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


def configure_account_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the explicit identity and test vault expected by build_daemon."""
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(TEST_EXCHANGE_ACCOUNT_ID))
    monkeypatch.setenv("BFX_VAULT_KEK", TEST_VAULT_KEK_B64)


def configure_canary_wiring_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Provide a test-only canary boot stub for non-preflight wiring tests.

    These tests exercise daemon construction after the shared preflight.  The
    dedicated Halt 2 tests cover the real verifier; keeping this stub local to
    test fixtures prevents unrelated wiring tests from needing production
    evidence files or a durable permit row.
    """
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_CANARY_ACCOUNT_ID", str(TEST_EXCHANGE_ACCOUNT_ID))
    monkeypatch.setenv("BFX_CANARY_ENVIRONMENT", "prod")
    monkeypatch.setenv("BFX_CANARY_SYMBOL", "fUST")
    monkeypatch.setenv("BFX_CANARY_CELL", "fUST_a30")
    monkeypatch.setenv("BFX_CANARY_STRATEGY", "rate_percentile")
    monkeypatch.setenv("BFX_CANARY_AMOUNT_USDT", "150")
    monkeypatch.setenv("BFX_CANARY_CAP_USDT", "500")
    monkeypatch.setenv("BFX_CANARY_MAX_EVIDENCE_AGE_SECONDS", "300")
    monkeypatch.setenv(
        "BFX_CANARY_PERMIT_ID", "33333333-3333-3333-3333-333333333333"
    )
    monkeypatch.setenv("BFX_EXPECTED_IMAGE_DIGEST", "sha256:test")
    monkeypatch.setenv("BFX_PROJECTOR_VERSION", "execution-state-v1")
    monkeypatch.setenv("BFX_HALT2_EVIDENCE_REPORT", str(tmp_path / "halt2-stub.json"))

    import scripts.halt2_cutover as halt2_cutover
    import scripts.run_canary_preflight as canary_preflight

    monkeypatch.setattr(
        halt2_cutover,
        "_load_evidence",
        lambda _path: SimpleNamespace(
            exchange_account_id=str(TEST_EXCHANGE_ACCOUNT_ID),
            deployment_environment="prod",
        ),
    )

    async def _preflight_stub(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(canary_preflight, "verify_canary_preflight", _preflight_stub)


async def seed_exchange_account(engine: AsyncEngine) -> None:
    """Provision the account/credential rows after a test schema is created."""
    envelope = encrypt_secret_with_aad(
        "test_secret", aad=str(TEST_EXCHANGE_ACCOUNT_ID), kek=TEST_VAULT_KEK
    )
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


__all__ = [
    "TEST_EXCHANGE_ACCOUNT_ID",
    "TEST_VAULT_KEK_B64",
    "configure_account_env",
    "configure_canary_wiring_env",
    "seed_exchange_account",
]
