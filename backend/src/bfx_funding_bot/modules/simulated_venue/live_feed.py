"""The live `MarketFeed`: public market data held in memory, filled by its own task.

The venue reads the feed inside its request lock (`MarketFeed` contract), so `book()`,
`trades()` and `complete_through()` only read the buffers below and never await a fetcher.
Everything that touches the network sits behind an injected seam, driven from outside or
from `run()`:

- books: `fetch_book(symbol)` returns the newest book the process holds locally (a
  WebSocket-maintained store), or None; `run()` copies it into the buffer every second.
- trades, WS first: a stream source (built in `apps` on the public WS `trades` channel)
  calls `ingest()` for every executed trade and `alive()` for every frame it receives,
  heartbeats included, and tells the feed when the stream `stream_connected()` /
  `stream_disconnected()`.
- trades, REST only for gaps: `fetch_trades(symbol, since_ms)` (inclusive) is called by
  `run()` once after every (re)connection, from the watermark where it stopped (or the
  start-up window), and nowhere else. REST is a per-IP resource shared with the live bot.

Event-time watermark (`complete_through`): the feed claims completeness only where it can
prove it. While the stream is connected and a symbol has no open gap, every frame at local
time T proves completeness up to `T - watermark_lag_ms` (frames are ordered; the lag
absorbs clock skew and delivery delay). On disconnect the watermark stops; it resumes only
after the gap backfill of that symbol succeeded. Until the first backfill it is None.

Admission is by trade id, per symbol and pruned with the buffer, so overlapping fetches,
equal timestamps and late or out-of-order arrivals are each admitted exactly once.

A fetcher that raises or runs past `fetch_deadline_s` is logged, counted in `failures` and
reported to `on_failure`; the buffers keep what they had, so a dead source ages the book
until the venue answers "no market data" and freezes the watermark, never invents data.

The module imports nothing of `external`: the sources are built in `apps`.
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
    fetch_deadline_s: float = 20.0
    # How long a failed gap backfill waits before it is tried again (rate-limit friendly).
    backfill_retry_s: float = 15.0
    watermark_lag_ms: int = 2_000
    # How far back the first backfill reaches, and how long the buffer keeps trades. An offer
    # that rests longer than the retention loses the volume that would have filled it.
    backfill_ms: int = 3_600_000
    retention_ms: int = 72 * 3_600_000

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("a live feed needs at least one symbol")
        if min(self.book_interval_s, self.fetch_deadline_s, self.backfill_retry_s) <= 0:
            raise ValueError("feed intervals and deadline must be positive")
        if self.watermark_lag_ms < 0 or self.backfill_ms < 0 or self.retention_ms < self.backfill_ms:
            raise ValueError("retention must cover the backfill window")


class LiveMarketFeed:
    def __init__(
        self, *, config: LiveFeedConfig, fetch_book: BookFetcher, fetch_trades: TradesFetcher,
        clock_ms: Callable[[], int], on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._fetch_book = fetch_book
        self._fetch_trades = fetch_trades
        self._clock_ms = clock_ms
        self._on_failure = on_failure
        self._books: dict[str, deque[BookSnapshot]] = {s: deque(maxlen=_BOOK_HISTORY)
                                                       for s in config.symbols}
        # Per symbol: trades ordered by (mts, id), and the ids held (pruned with the rows).
        self._trades: dict[str, list[PublicTrade]] = {s: [] for s in config.symbols}
        self._ids: dict[str, set[int]] = {s: set() for s in config.symbols}
        self._complete: dict[str, int] = {}
        self._connected = False
        self._gap: set[str] = set(config.symbols)
        self._retry_at: dict[str, float] = {}
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

    async def complete_through(self, symbol: str) -> int | None:
        return self._complete.get(symbol)

    async def book(self, symbol: str, *, at_ms: int) -> BookSnapshot | None:
        for snapshot in reversed(self._books.get(symbol, ())):
            if snapshot.captured_at_ms <= at_ms:
                return snapshot
        return None

    def has_books(self) -> bool:
        """Every configured symbol has at least one book in the buffer."""
        return all(self._books[s] for s in self._config.symbols)

    # -- the stream source's calls (synchronous: they only touch memory) ---------------

    def stream_connected(self) -> None:
        """The stream is subscribed: everything since the newest held trade must be filled."""
        self._connected = True
        self._gap = set(self._config.symbols)
        self._retry_at.clear()

    def stream_disconnected(self) -> None:
        """The watermark stops here; the next connection re-opens the gap of every symbol."""
        self._connected = False
        self._gap = set(self._config.symbols)

    def ingest(self, symbol: str, trades: Sequence[PublicTrade]) -> None:
        """Admit each trade once, by id (the stream's, and a backfill's, alike)."""
        if symbol not in self._trades or not trades:
            return
        rows, ids = self._trades[symbol], self._ids[symbol]
        new = [t for t in trades if t.id not in ids]
        if not new:
            return
        ids.update(t.id for t in new)
        rows.extend(new)
        rows.sort(key=lambda t: (t.mts, t.id))

    def alive(self, symbol: str, at_ms: int) -> None:
        """A frame (trade or heartbeat) of `symbol` arrived at local time `at_ms`."""
        if self._connected and symbol not in self._gap and symbol in self._trades:
            mark = at_ms - self._config.watermark_lag_ms
            if mark > self._complete.get(symbol, -1):
                self._complete[symbol] = mark

    # -- the feed's own task ----------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Keep the book buffer current and fill trade gaps until `stop` is set."""
        while not stop.is_set():
            await self.refresh_books()
            await self.backfill_gaps()
            for symbol in self._config.symbols:
                self._prune(symbol, self._clock_ms())
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

    async def backfill_gaps(self) -> None:
        """REST backfill of every open gap, only while the stream is connected.

        The request is made AFTER the subscription, so a trade is either in the stream or in
        the reply. The gap closes (and the watermark resumes) at the request's start time.
        """
        if not self._connected:
            return
        for symbol in sorted(self._gap):
            if self._monotonic() < self._retry_at.get(symbol, 0.0):
                continue
            started = self._clock_ms()
            # From the last instant proven complete (frozen since the stream dropped), not
            # from the newest trade held: live frames received during the gap are newer than
            # what the gap lost. What overlaps is admitted once, by id.
            since = self._complete.get(symbol, started - self._config.backfill_ms)
            fetched = await self._guarded(
                f"trades {symbol}", self._fetch_trades(symbol, since))
            if fetched is None:
                self._retry_at[symbol] = self._monotonic() + self._config.backfill_retry_s
                continue
            self.ingest(symbol, fetched)
            self._prune(symbol, started)
            if not self._connected:
                continue  # the stream dropped meanwhile: the gap is open again
            self._gap.discard(symbol)
            mark = started - self._config.watermark_lag_ms
            if mark > self._complete.get(symbol, -1):
                self._complete[symbol] = mark

    async def _guarded[T](self, what: str, call: Awaitable[T]) -> T | None:
        try:
            async with asyncio.timeout(self._config.fetch_deadline_s):
                return await call
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            if self._on_failure is not None:
                self._on_failure(what.split(" ")[0])
            log.warning("simulated_venue_feed_fetch_failed what=%s error=%r", what, exc)
            return None

    def _prune(self, symbol: str, now_ms: int) -> None:
        horizon = now_ms - self._config.retention_ms
        rows = self._trades[symbol]
        cut = bisect_right(rows, horizon, key=lambda t: t.mts)
        if cut:
            self._ids[symbol].difference_update(t.id for t in rows[:cut])
            del rows[:cut]
