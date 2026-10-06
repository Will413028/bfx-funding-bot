"""Reusable identity rows for daemon SQLite wiring tests."""
from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path
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


async def stamp_schema_head(engine: AsyncEngine, *, realm: str | None = None) -> None:
    """Record this build's schema head, the capital authority epochs a fresh database holds
    at head (the seeded ``legacy`` row and the ``ledger`` genesis, ``b1c2d3e4f5a6``) and the
    database realm (``ci`` unless a test boots as another realm), as ``alembic upgrade``
    plus the owner's one-time stamp would on Postgres."""
    import os

    from sqlalchemy import inspect, text

    from bfx_funding_bot.apps.authority_support import GENESIS_ACTOR
    from bfx_funding_bot.core.schema_head import build_head
    realm = realm or os.environ.get("BFX_DEPLOYMENT_ENV", "").strip() or "ci"
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
                           {"head": build_head()})
        if await conn.run_sync(lambda sync: inspect(sync).has_table("capital_authority_epoch")):
            await conn.execute(text(
                "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
                "SELECT 1, 'legacy', 0, 'test', 'initial authority' "
                "WHERE NOT EXISTS (SELECT 1 FROM capital_authority_epoch)"))
            await conn.execute(text(
                "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
                "SELECT 2, 'ledger', 0, :actor, 'genesis: no legacy history' "
                "WHERE (SELECT max(epoch_seq) FROM capital_authority_epoch) = 1"),
                {"actor": GENESIS_ACTOR})
        if await conn.run_sync(lambda sync: inspect(sync).has_table("database_realm")):
            await conn.execute(text(
                "INSERT INTO database_realm (realm, stamped_at_ms, actor) "
                "SELECT :realm, 0, 'test' WHERE NOT EXISTS (SELECT 1 FROM database_realm)"),
                {"realm": realm})


def configure_account_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the explicit identity and test vault expected by build_daemon."""
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(TEST_EXCHANGE_ACCOUNT_ID))
    monkeypatch.setenv("BFX_VAULT_KEK", TEST_VAULT_KEK_B64)


def configure_live_wiring_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Normal live construction: explicit account, live phase."""
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_PHASE", "live")
    monkeypatch.delenv("BFX_ALLOCATION_CAP_USDT", raising=False)


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
        from bfx_funding_bot.modules.ledger import Scope
        from bfx_funding_bot.modules.ledger.wiring import build_policy_store
        from bfx_funding_bot.modules.trading import CapitalPolicy
        for environment in ("ci", "prod", "shadow"):
            store = build_policy_store(Scope(TEST_EXCHANGE_ACCOUNT_ID, environment))
            async with factory.begin() as session:
                for symbol in ("fUST", "fUSD"):
                    await store.apply_policy(session, symbol=symbol,
                        policy=CapitalPolicy(enabled=symbol == "fUST"), expected_revision=0,
                        source={"synthetic_fixture": True})


_SAFETY_LIVE = Path(__file__).parents[3] / "configs" / "safety.live.yaml"


def live_construction_env(
    monkeypatch: pytest.MonkeyPatch, database_url: str, **extra: str,
) -> None:
    """The environment of a Bitfinex-venue boot: live phase, ci realm, book-guarded policy.

    ``BFX_EXECUTOR`` and the legacy money env are not part of it (the first is ignored, the
    second refused); a test that needs either sets it itself.
    """
    import os

    configure_account_env(monkeypatch)
    for name in list(os.environ):
        if name.startswith("BFX_CANARY_") or name in (
            "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
            "BFX_EXECUTOR", "BFX_FILL_TRACKER_ENABLED", "BFX_WS_CLIENT_ENABLED",
        ):
            monkeypatch.delenv(name)
    values = {
        "BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": "ci",
        "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
        "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        "BFX_SERVICE_VERSION": "test", "BFX_HEALTHZ_PORT": "0",
        "BFX_SAFETY_CONFIG": str(_SAFETY_LIVE),
        "DATABASE_URL": database_url, **extra,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def write_cells_yaml(tmp_path: Path, *, symbol: str = "fUST", period_agg: str = "a30") -> Path:
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text(f"""
cells:
  - strategy: rate_percentile
    symbol: {symbol}
    period_agg: {period_agg}
    timeframe: 1h
    params: {{percentile: 75, lookback_hours: 5}}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    return yaml_path


async def boot_live_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock, *, cells_yaml: Path | None = None,
    name: str = "daemon", extra_env: dict[str, str] | None = None, before_boot=None,
):  # type: ignore[no-untyped-def]
    """Construct (not run) a Bitfinex-venue daemon on file-based sqlite; public GETs are mocked.

    Returns ``(daemon, engine)``; the caller disposes the engine. Use this where a test needs
    the composed daemon only because composing it is the cheapest way to get its objects.
    ``before_boot(engine)`` may seed more rows (an operator's ACTIVE, say) before the boot.
    """
    import re

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.apps.bot import build_daemon
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    url = f"sqlite+aiosqlite:///{tmp_path / name}.db"
    live_construction_env(monkeypatch, url, **(extra_env or {}))
    engine = make_async_engine_from_url(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine)
    if before_boot is not None:
        await before_boot(engine)
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[], is_reusable=True, is_optional=True,
    )
    daemon = await build_daemon(cells_yaml_path=cells_yaml or write_cells_yaml(tmp_path), skip_ws=True)
    return daemon, engine


def go_offline(daemon) -> None:  # type: ignore[no-untyped-def]
    """Detach what would reach the network when ``daemon.run()`` runs in a test.

    The book and auth WebSockets become idle tasks that exit on the stop event, and the
    venue observation loops (boot recovery, periodic reconcile, income syncs) are dropped.
    Everything else (scheduler, writer, healthz, workers, supervision) stays as composed.
    """
    async def idle(stop_event) -> None:  # type: ignore[no-untyped-def]
        await stop_event.wait()

    if daemon.funding_book_service is not None:
        daemon.funding_book_service.run = idle
    if daemon.ws_dispatcher is not None:
        daemon.ws_dispatcher.run = idle
    daemon.boot_recovery = None
    daemon.periodic_reconcile = None
    daemon.interest_ledger_sync = None
    daemon.credit_history_sync = None


__all__ = [
    "TEST_EXCHANGE_ACCOUNT_ID",
    "TEST_VAULT_KEK_B64",
    "boot_live_construction",
    "configure_account_env",
    "configure_live_wiring_env",
    "go_offline",
    "live_construction_env",
    "seed_exchange_account",
    "stamp_schema_head",
    "write_cells_yaml",
]
