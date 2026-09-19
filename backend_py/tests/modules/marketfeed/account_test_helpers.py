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


def configure_release_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic measured identity for construction-only tests; never production fallback."""
    from bfx_funding_bot.core.release_identity import (
        ReleaseManifest,
        ReleaseRuntime,
        VerifiedRelease,
    )
    manifest = ReleaseManifest(version=1, release_id="fixture", source_revision="a" * 40,
        platform="linux/arm64", docker_image_id="sha256:" + "b" * 64,
        oci_manifest_digest=None, inventory={}, python_inventory={}, environment={}, schema_head="b4e6f8a0c203",
        projector_version="execution-state-v1")
    proof = VerifiedRelease(manifest=manifest, release_digest="c" * 64,
        config_digest="d" * 64, launch_id="e" * 32)
    monkeypatch.setattr(ReleaseRuntime, "from_environment", classmethod(lambda cls: SimpleNamespace(verify=lambda: proof)))


def configure_account_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the explicit identity and test vault expected by build_daemon."""
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(TEST_EXCHANGE_ACCOUNT_ID))
    monkeypatch.setenv("BFX_VAULT_KEK", TEST_VAULT_KEK_B64)


def configure_canary_wiring_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Historical fixture name; construction now uses normal live plus measured release."""
    configure_account_env(monkeypatch)
    configure_release_runtime(monkeypatch)
    monkeypatch.setenv("BFX_PHASE", "live")
    monkeypatch.delenv("BFX_ALLOCATION_CAP_USDT", raising=False)
    monkeypatch.setenv("BFX_PROJECTOR_VERSION", "execution-state-v1")
    monkeypatch.setenv("BFX_HALT2_EVIDENCE_REPORT", str(tmp_path / "halt2-stub.json"))


async def seed_exchange_account(engine: AsyncEngine, *, capital_policies: bool = True) -> None:
    """Provision synthetic identity and explicit applied policies, never an env fallback."""
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
    "configure_canary_wiring_env",
    "seed_exchange_account",
]
