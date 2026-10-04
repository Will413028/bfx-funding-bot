"""``apps/venue.py``: the Bitfinex wiring, and the fetchers that feed the simulated venue.

Mutations (one at a time; revert after each): the trades fetcher keeps rows at or before
``since_ms`` (``test_the_trades_fetcher_returns_only_newer_rows_and_pages``); it takes the
signed amount instead of its magnitude (same test); the book fetcher stamps the snapshot
with a time other than the venue's last confirmation (``test_the_book_fetcher_*``).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.apps import venue as venue_module
from bfx_funding_bot.apps.venue import VenueFeedTask, _book_fetcher, _trades_fetcher
from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel, FundingTrade
from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookStore
from bfx_funding_bot.modules.simulated_venue import LiveFeedConfig, LiveMarketFeed

T0 = 1_704_067_200_000


class _Rest:
    def __init__(self, rows: list[FundingTrade]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []

    async def get_funding_trades(self, *, symbol: str, start: int, limit: int) -> list[FundingTrade]:
        self.calls.append((start, limit))
        return [r for r in self.rows if r.mts >= start][:limit]


async def test_the_trades_fetcher_returns_only_newer_rows_and_pages(monkeypatch) -> None:
    monkeypatch.setattr(venue_module, "_TRADES_PAGE", 2)
    rows = [FundingTrade(i, T0 + i * 10, -100.0 - i, 0.0002, 2) for i in range(1, 6)]
    rest = _Rest(rows)
    fetched = await _trades_fetcher(rest)("fUST", T0 + 10)  # type: ignore[arg-type]
    assert sorted(t.mts for t in fetched) == [T0 + 20, T0 + 30, T0 + 40, T0 + 50]
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
    async def fetch_book(symbol: str):  # type: ignore[no-untyped-def]
        from tests.modules.simulated_venue.helpers import book
        return book(symbol, T0) if symbol == "fUST" or ready.is_set() else None

    async def fetch_trades(symbol: str, since_ms: int):  # type: ignore[no-untyped-def]
        return []

    ready = asyncio.Event()
    feed = LiveMarketFeed(config=LiveFeedConfig(symbols=("fUST", "fUSD"), book_interval_s=0.01),
                          fetch_book=fetch_book, fetch_trades=fetch_trades, clock_ms=lambda: T0)

    class _Service:
        async def run(self, stop: asyncio.Event) -> None:
            await stop.wait()

    task = VenueFeedTask(book_service=_Service(), feed=feed)  # type: ignore[arg-type]
    stop = asyncio.Event()
    running = asyncio.create_task(task.run(stop))
    try:
        assert await task.wait_ready(stop, timeout_s=0.05) is False  # fUSD has no book
        ready.set()
        assert await task.wait_ready(stop, timeout_s=30) is True
    finally:
        stop.set()
        await running


@pytest.mark.parametrize("name", ["PaperPositionLedger", "OfferRegistry", "EventStorePersister"])
def test_the_venue_module_names_no_legacy_class(name: str) -> None:
    import inspect

    assert name not in inspect.getsource(venue_module)
