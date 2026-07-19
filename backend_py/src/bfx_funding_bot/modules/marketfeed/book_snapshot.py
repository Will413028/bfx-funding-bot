"""BookSnapshotWriter — periodic funding-book depth recording.

Rationale: Bitfinex provides no historical book; the only way to ever backtest
book-aware strategies (spike-rung ladder) is to start recording now. Hourly
snapshots × 2 symbols ≈ 50 MB/year.

Fail-open everywhere: a snapshot is an observation, never worth crashing the
daemon over. Enabled via BFX_BOOK_SNAPSHOT_ENABLED (default false; flags live
in deploy/vm/canary.env), cadence via BFX_BOOK_SNAPSHOT_INTERVAL_S.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel
from bfx_funding_bot.modules.marketfeed.tables import FundingBookSnapshotRow

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 3600
_BOOK_LEN = 25


class _RestProtocol(Protocol):
    async def get_funding_book(
        self, *, symbol: str, length: int = 25
    ) -> list[FundingBookLevel]: ...


def _dec(x: float) -> Decimal:
    return Decimal(str(x))


class BookSnapshotWriter:
    def __init__(
        self,
        *,
        rest: _RestProtocol,
        session_factory: async_sessionmaker[AsyncSession],
        symbols: Sequence[str],
        interval_s: int = DEFAULT_INTERVAL_S,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._rest = rest
        self._sf = session_factory
        self._symbols = tuple(symbols)
        self._interval_s = interval_s
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def tick(self) -> None:
        """One snapshot round across all symbols. Never raises."""
        for symbol in self._symbols:
            try:
                levels = await self._rest.get_funding_book(
                    symbol=symbol, length=_BOOK_LEN
                )
                await self._write(symbol, levels)
            except Exception:
                log.exception("book_snapshot_failed symbol=%s (fail-open)", symbol)

    async def _write(self, symbol: str, levels: list[FundingBookLevel]) -> None:
        asks = [lv for lv in levels if lv.amount > 0]
        bids = [lv for lv in levels if lv.amount < 0]
        row = FundingBookSnapshotRow(
            symbol=symbol,
            captured_at_ms=self._clock(),
            best_ask_rate=_dec(min(lv.rate for lv in asks)) if asks else None,
            best_bid_rate=_dec(max(lv.rate for lv in bids)) if bids else None,
            ask_depth=sum((_dec(lv.amount) for lv in asks), Decimal("0")),
            bid_depth=sum((_dec(abs(lv.amount)) for lv in bids), Decimal("0")),
            payload={
                "asks": [[lv.rate, lv.period, lv.count, lv.amount] for lv in asks],
                "bids": [[lv.rate, lv.period, lv.count, abs(lv.amount)] for lv in bids],
            },
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Daemon sub-task loop: tick immediately, then every interval_s."""
        log.info(
            "book_snapshot_writer_started symbols=%s interval_s=%d",
            ",".join(self._symbols), self._interval_s,
        )
        while not stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
