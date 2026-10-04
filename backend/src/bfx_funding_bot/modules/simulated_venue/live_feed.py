"""The live `MarketFeed`: public market data held in memory, filled by its own task.

The venue reads the feed inside its request lock (`MarketFeed` contract), so `book()` and
`trades()` only read the buffer below and never await a fetcher. Everything that touches
the network is an injected fetcher, called only from `run()`:

- `fetch_book(symbol)` returns the newest book the process holds, or None. It is meant to
  read a local, WebSocket-maintained store (WS first); it must not poll the venue.
- `fetch_trades(symbol, since_ms)` returns public trades with `mts > since_ms` (the
  feed keeps only what is newer than what it holds, so an overlapping fetch is harmless). It may do
  REST: `run()` calls it once per symbol at start-up (backfill) and then every
  `trades_interval_s`, which is the only REST traffic this feed adds to the shared per-IP
  public rate limit.

A fetcher that raises or runs past `fetch_deadline_s` is logged and counted in
`failures`; the buffer keeps what it had, so a dead source ages the book until the venue
answers "no market data" (an internal failure), never a made-up price.

The module imports nothing of `external`: the fetchers are built in `apps`.
"""
from __future__ import annotations

import asyncio
import logging
from bisect import bisect_right
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from bfx_funding_bot.modules.simulated_venue.contracts import BookSnapshot, PublicTrade

log = logging.getLogger(__name__)

BookFetcher = Callable[[str], Awaitable[BookSnapshot | None]]
TradesFetcher = Callable[[str, int], Awaitable[Sequence[PublicTrade]]]

_BOOK_HISTORY = 64  # snapshots kept per symbol, so `book(at_ms=...)` can answer an older ask


@dataclass(frozen=True, slots=True)
class LiveFeedConfig:
    symbols: tuple[str, ...]
    book_interval_s: float = 1.0
    trades_interval_s: float = 30.0
    fetch_deadline_s: float = 20.0
    # How far back start-up fetches trades, and how long the buffer keeps them. An offer
    # that rests longer than the retention loses the volume that would have filled it.
    backfill_ms: int = 3_600_000
    retention_ms: int = 72 * 3_600_000

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("a live feed needs at least one symbol")
        if min(self.book_interval_s, self.trades_interval_s, self.fetch_deadline_s) <= 0:
            raise ValueError("feed intervals and deadline must be positive")
        if self.backfill_ms < 0 or self.retention_ms < self.backfill_ms:
            raise ValueError("retention must cover the backfill window")


class LiveMarketFeed:
    def __init__(
        self, *, config: LiveFeedConfig, fetch_book: BookFetcher, fetch_trades: TradesFetcher,
        clock_ms: Callable[[], int],
    ) -> None:
        self._config = config
        self._fetch_book = fetch_book
        self._fetch_trades = fetch_trades
        self._clock_ms = clock_ms
        self._books: dict[str, deque[BookSnapshot]] = {s: deque(maxlen=_BOOK_HISTORY)
                                                       for s in config.symbols}
        # Per symbol: trades ordered by mts (ties in arrival order), and the newest mts seen.
        self._trades: dict[str, list[PublicTrade]] = {s: [] for s in config.symbols}
        self._newest_mts: dict[str, int] = {}
        self.failures = 0

    # -- the MarketFeed contract: buffer reads only -----------------------------------

    async def trades(
        self, symbol: str, *, after_ms: int, through_ms: int,
    ) -> Sequence[PublicTrade]:
        rows = self._trades.get(symbol, [])
        # Rows are sorted by mts: slice by position rather than scanning the buffer.
        low = bisect_right(rows, after_ms, key=lambda t: t.mts)
        high = bisect_right(rows, through_ms, key=lambda t: t.mts)
        return rows[low:high]

    async def book(self, symbol: str, *, at_ms: int) -> BookSnapshot | None:
        for snapshot in reversed(self._books.get(symbol, ())):
            if snapshot.captured_at_ms <= at_ms:
                return snapshot
        return None

    def has_books(self) -> bool:
        """Every configured symbol has at least one book in the buffer."""
        return all(self._books[s] for s in self._config.symbols)

    # -- the feed's own task ----------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Backfill trades once, then keep both buffers current until `stop` is set."""
        await self.refresh_trades(backfill=True)
        next_trades = self._monotonic() + self._config.trades_interval_s
        while not stop.is_set():
            await self.refresh_books()
            if self._monotonic() >= next_trades:
                await self.refresh_trades()
                next_trades = self._monotonic() + self._config.trades_interval_s
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._config.book_interval_s)
            except TimeoutError:
                continue

    @staticmethod
    def _monotonic() -> float:
        return asyncio.get_running_loop().time()

    async def refresh_books(self) -> None:
        for symbol in self._config.symbols:
            snapshot = await self._guarded(f"book {symbol}", self._fetch_book(symbol))
            if snapshot is None:
                continue
            history = self._books[symbol]
            if history and snapshot.captured_at_ms <= history[-1].captured_at_ms:
                continue  # nothing newer than what the buffer holds
            history.append(snapshot)

    async def refresh_trades(self, *, backfill: bool = False) -> None:
        now = self._clock_ms()
        for symbol in self._config.symbols:
            since = (now - self._config.backfill_ms if backfill
                     else self._newest_mts.get(symbol, now - self._config.backfill_ms))
            fetched = await self._guarded(
                f"trades {symbol}", self._fetch_trades(symbol, since))
            if fetched:
                self._admit(symbol, fetched)
            self._prune(symbol, now)

    async def _guarded[T](self, what: str, call: Awaitable[T]) -> T | None:
        try:
            async with asyncio.timeout(self._config.fetch_deadline_s):
                return await call
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            log.warning("simulated_venue_feed_fetch_failed what=%s error=%r", what, exc)
            return None

    def _admit(self, symbol: str, fetched: Sequence[PublicTrade]) -> None:
        """Keep only trades newer than the newest already held, so an overlapping fetch is
        idempotent while identical trades inside one fetch (distinct executions) all count."""
        rows = self._trades[symbol]
        floor = self._newest_mts.get(symbol)
        new = [t for t in fetched if floor is None or t.mts > floor]
        if not new:
            return
        rows.extend(new)
        rows.sort(key=lambda t: t.mts)
        self._newest_mts[symbol] = rows[-1].mts

    def _prune(self, symbol: str, now_ms: int) -> None:
        horizon = now_ms - self._config.retention_ms
        rows = self._trades[symbol]
        cut = bisect_right(rows, horizon, key=lambda t: t.mts)
        if cut:
            del rows[:cut]
