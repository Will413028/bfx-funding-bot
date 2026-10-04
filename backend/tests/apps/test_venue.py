"""``apps/venue.py``: what each venue's wiring carries, and the fetchers that feed the simulator.

Mutations (one at a time; revert after each): the simulated credentials open the vault
(``test_simulated_credentials_*``); the trades fetcher stops paging or takes the signed amount
(``test_the_trades_fetcher_*``); the book fetcher stamps the snapshot with another time
(``test_the_book_fetcher_*``).
"""
from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.apps import venue as venue_module
from bfx_funding_bot.apps.venue import (
    BITFINEX_CAPABILITIES,
    SIMULATED_CAPABILITIES,
    VenueFeedTask,
    _book_fetcher,
    _throwaway_credentials,
    _trades_fetcher,
    _vault_credentials,
)
from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel, FundingTrade
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookStore
from bfx_funding_bot.modules.simulated_venue import LiveFeedConfig, LiveMarketFeed

T0 = 1_704_067_200_000
ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
KEK_B64 = base64.b64encode(bytes(range(32))).decode()


def test_the_capabilities_each_wiring_states() -> None:
    assert (BITFINEX_CAPABILITIES.auth_ws, BITFINEX_CAPABILITIES.rest_fill_tracker) == (
        "required", True)
    assert (SIMULATED_CAPABILITIES.auth_ws, SIMULATED_CAPABILITIES.rest_fill_tracker) == (
        "forbidden", False)


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
                lifecycle_status=credential,
                verified_at=datetime.now(UTC) if credential == "active" else None))


async def test_the_bitfinex_wiring_reads_the_vault(factory, monkeypatch) -> None:
    await _seed(factory)
    monkeypatch.setenv("BFX_VAULT_KEK", KEK_B64)
    async with factory() as session:
        credentials = await _vault_credentials(session, ACCOUNT)
    assert (credentials.api_key, credentials.api_secret) == ("account-key", "account-secret")


@pytest.mark.parametrize(("credential", "kek", "message"), [
    (None, True, "active Bitfinex credential"),
    ("pending", True, "active Bitfinex credential"),
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


class _Rest:
    def __init__(self, rows: list[FundingTrade]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []

    async def get_funding_trades(self, *, symbol: str, start: int, limit: int) -> list[FundingTrade]:
        self.calls.append((start, limit))
        return [r for r in self.rows if r.mts >= start][:limit]


async def test_the_trades_fetcher_is_inclusive_pages_and_keeps_the_ids(monkeypatch) -> None:
    monkeypatch.setattr(venue_module, "_TRADES_PAGE", 2)
    rows = [FundingTrade(i, T0 + i * 10, -100.0 - i, 0.0002, 2) for i in range(1, 6)]
    rest = _Rest(rows)
    fetched = await _trades_fetcher(rest)("fUST", T0 + 10)  # type: ignore[arg-type]
    assert sorted(t.id for t in fetched) == [1, 2, 3, 4, 5]  # mts >= since: inclusive
    assert all(t.amount > 0 for t in fetched)  # the magnitude: the sign names the taker
    assert fetched[0].rate == Decimal("0.0002") and fetched[0].period == 2
    assert len(rest.calls) >= 2  # more than one page was needed


async def test_a_page_of_one_millisecond_cannot_loop_forever(monkeypatch) -> None:
    monkeypatch.setattr(venue_module, "_TRADES_PAGE", 2)
    rest = _Rest([FundingTrade(i, T0 + 1, 5.0, 0.0002, 2) for i in range(1, 6)])
    fetched = await _trades_fetcher(rest)("fUST", T0)  # type: ignore[arg-type]
    assert len(rest.calls) <= venue_module._TRADES_MAX_PAGES
    assert len(fetched) == 2  # honest about what one page of one millisecond could show


async def test_the_book_fetcher_reads_the_local_store_and_stamps_the_last_confirmation() -> None:
    clock = [T0]
    store = FundingBookStore(max_age_seconds=30, clock=lambda: clock[0])
    fetch = _book_fetcher(store, lambda: clock[0])
    assert await fetch("fUST") is None  # no baseline yet
    store.apply_snapshot("fUST", [
        FundingBookLevel(0.0003, 2, 3, 5000.0), FundingBookLevel(0.0002, 2, 2, -5000.0)])
    clock[0] = T0 + 5_000
    store.apply_sequence("fUST", 1)  # a heartbeat: the venue confirmed the book again
    snapshot: Any = await fetch("fUST")
    assert snapshot.captured_at_ms == T0 + 5_000
    assert snapshot.asks == ((Decimal("0.0003"), 2, Decimal(5000)),)  # asks only
    clock[0] = T0 + 60_000  # silence past the store's max age: nothing usable
    assert await fetch("fUST") is None


async def test_the_venue_feed_task_is_ready_once_every_symbol_has_a_book() -> None:
    from tests.modules.simulated_venue.helpers import book

    ready = asyncio.Event()

    async def fetch_book(symbol: str):  # type: ignore[no-untyped-def]
        return book(symbol, T0) if symbol == "fUST" or ready.is_set() else None

    async def fetch_trades(symbol: str, since_ms: int):  # type: ignore[no-untyped-def]
        return []

    feed = LiveMarketFeed(config=LiveFeedConfig(symbols=("fUST", "fUSD"), book_interval_s=0.01),
                          fetch_book=fetch_book, fetch_trades=fetch_trades, clock_ms=lambda: T0)

    class _Idle:
        async def run(self, stop: asyncio.Event) -> None:
            await stop.wait()

    task = VenueFeedTask(book_service=_Idle(), trades_stream=_Idle(), feed=feed)  # type: ignore[arg-type]
    assert task.name == "venue_feed"
    stop = asyncio.Event()
    running = asyncio.create_task(task.run(stop))
    try:
        assert await task.wait_ready(stop, timeout_s=0.05) is False  # fUSD has no book
        ready.set()
        assert await task.wait_ready(stop, timeout_s=30) is True
    finally:
        stop.set()
        await running
