"""Fixture `MarketFeed`: replays recorded trades and book snapshots. No network."""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from bfx_funding_bot.modules.simulated_venue.contracts import BookSnapshot, PublicTrade


class FixtureMarketFeed:
    def __init__(
        self, *, trades: Iterable[tuple[str, PublicTrade]] = (),
        books: Iterable[BookSnapshot] = (),
    ) -> None:
        self._trades: list[tuple[str, PublicTrade]] = list(trades)
        self._books: list[BookSnapshot] = list(books)

    def add_trades(self, symbol: str, trades: Iterable[PublicTrade]) -> None:
        self._trades.extend((symbol, t) for t in trades)

    def add_book(self, book: BookSnapshot) -> None:
        self._books.append(book)

    async def trades(
        self, symbol: str, *, after_ms: int, through_ms: int,
    ) -> Sequence[PublicTrade]:
        return [t for s, t in self._trades if s == symbol and after_ms < t.mts <= through_ms]

    async def book(self, symbol: str, *, at_ms: int) -> BookSnapshot | None:
        eligible = [b for b in self._books if b.symbol == symbol and b.captured_at_ms <= at_ms]
        return max(eligible, key=lambda b: b.captured_at_ms, default=None)
