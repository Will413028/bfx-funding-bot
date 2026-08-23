"""Canonical, fail-closed live funding-book snapshot provider."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from bfx_funding_bot.external.bitfinex.funding_book_ws import funding_book_checksum
from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel

log = logging.getLogger(__name__)


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class ExactPeriodBook:
    bids: tuple[FundingBookLevel, ...]
    asks: tuple[FundingBookLevel, ...]


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    snapshot_id: str
    symbol: str
    bids: tuple[FundingBookLevel, ...]
    asks: tuple[FundingBookLevel, ...]
    captured_at_ms: int
    received_at_ms: int
    source: Literal["ws", "rest_reconciled"]
    sequence_valid: bool
    checksum_valid: bool
    sequence: int | None

    def is_fresh(self, *, symbol: str, now_ms: int, max_age_ms: int) -> bool:
        return (
            symbol == self.symbol
            and self.captured_at_ms <= now_ms
            and now_ms - self.captured_at_ms <= max_age_ms
            and self.sequence_valid
            and self.checksum_valid
        )

    def exact_period(self, *, period_days: int, amount: Decimal) -> ExactPeriodBook | None:
        bids = tuple(level for level in self.bids if level.period == period_days)
        asks = tuple(level for level in self.asks if level.period == period_days)
        bid_depth = sum((-_decimal(level.amount) for level in bids), Decimal("0"))
        ask_depth = sum((_decimal(level.amount) for level in asks), Decimal("0"))
        if (not bids and not asks) or max(bid_depth, ask_depth) < amount:
            return None
        return ExactPeriodBook(bids=bids, asks=asks)


class FundingBookProvider(Protocol):
    def snapshot(self, symbol: str, *, now_ms: int) -> MarketSnapshot | None: ...


@dataclass(slots=True)
class _BookState:
    bids: dict[tuple[float, int], FundingBookLevel] = field(default_factory=dict)
    asks: dict[tuple[float, int], FundingBookLevel] = field(default_factory=dict)
    captured_at_ms: int | None = None
    received_at_ms: int | None = None
    sequence: int | None = None
    generation: int = 0
    ws_snapshot_received: bool = False
    sequence_valid: bool = False
    checksum_valid: bool = False
    requires_reconciliation: bool = False
    source: Literal["ws", "rest_reconciled"] = "ws"


class FundingBookStore(FundingBookProvider):
    """Single in-memory source for live eligibility snapshots."""

    def __init__(self, *, max_age_seconds: float, clock: Callable[[], int] | None = None) -> None:
        self._max_age_ms = int(max_age_seconds * 1_000)
        self._clock = clock or (lambda: int(time.time() * 1_000))
        self._states: dict[str, _BookState] = {}

    def apply_snapshot(
        self,
        symbol: str,
        levels: Sequence[FundingBookLevel],
        *,
        sequence: int | None = None,
    ) -> None:
        state = self._states.setdefault(symbol, _BookState())
        state.bids, state.asks = self._split_levels(levels)
        state.captured_at_ms = self._clock()
        state.received_at_ms = self._clock()
        state.ws_snapshot_received = True
        state.source = "ws"
        state.sequence = sequence
        # The snapshot only establishes a sequence baseline. A subsequent
        # continuous sequence and matching checksum are required before use.
        state.sequence_valid = False
        state.checksum_valid = False
        state.generation += 1

    def apply_update(
        self, symbol: str, level: FundingBookLevel, *, sequence: int | None = None
    ) -> None:
        state = self._states.setdefault(symbol, _BookState())
        self._apply_sequence(state, sequence)
        side = state.asks if level.amount > 0 else state.bids
        key = (level.rate, level.period)
        if level.count == 0:
            state.bids.pop(key, None)
            state.asks.pop(key, None)
        else:
            side[key] = level
        state.captured_at_ms = self._clock()
        state.received_at_ms = self._clock()
        state.source = "ws"
        state.checksum_valid = False
        state.generation += 1

    def apply_sequence(self, symbol: str, sequence: int) -> None:
        state = self._states.setdefault(symbol, _BookState())
        self._apply_sequence(state, sequence)
        state.generation += 1

    def apply_checksum(
        self,
        symbol: str,
        *,
        checksum: int,
        expected: int | None = None,
        sequence: int | None = None,
    ) -> None:
        state = self._states.setdefault(symbol, _BookState())
        self._apply_sequence(state, sequence)
        computed = (
            expected
            if expected is not None
            else funding_book_checksum(
                bids=list(state.bids.values()), asks=list(state.asks.values())
            )
        )
        if checksum != computed:
            self._invalidate(state)
        elif state.sequence_valid and state.ws_snapshot_received:
            state.checksum_valid = True
        state.generation += 1

    def apply_rest_snapshot(
        self,
        symbol: str,
        levels: Sequence[FundingBookLevel],
        *,
        expected_generation: int | None = None,
    ) -> bool:
        state = self._states.setdefault(symbol, _BookState())
        if expected_generation is not None and state.generation != expected_generation:
            return False
        state.bids, state.asks = self._split_levels(levels)
        state.captured_at_ms = self._clock()
        state.received_at_ms = self._clock()
        state.source = "rest_reconciled"
        # REST levels replace the book, so all prior WS evidence is stale. A
        # new WS snapshot, contiguous sequence, and matching checksum must
        # establish eligibility after this rebase.
        state.sequence = None
        state.ws_snapshot_received = False
        state.sequence_valid = False
        state.checksum_valid = False
        state.requires_reconciliation = False
        state.generation += 1
        return True

    def mark_disconnected(self) -> None:
        for state in self._states.values():
            self._invalidate(state)
            state.generation += 1

    def generation(self, symbol: str) -> int:
        state = self._states.get(symbol)
        return state.generation if state is not None else 0

    def snapshot(self, symbol: str, *, now_ms: int) -> MarketSnapshot | None:
        state = self._states.get(symbol)
        if state is None or state.captured_at_ms is None or state.received_at_ms is None:
            return None
        snapshot = MarketSnapshot(
            snapshot_id=self._snapshot_id(symbol, state),
            symbol=symbol,
            bids=tuple(sorted(state.bids.values(), key=lambda level: level.rate, reverse=True)),
            asks=tuple(sorted(state.asks.values(), key=lambda level: level.rate)),
            captured_at_ms=state.captured_at_ms,
            received_at_ms=state.received_at_ms,
            source=state.source,
            sequence_valid=state.sequence_valid,
            checksum_valid=state.checksum_valid,
            sequence=state.sequence,
        )
        if state.requires_reconciliation or not snapshot.is_fresh(
            symbol=symbol, now_ms=now_ms, max_age_ms=self._max_age_ms
        ):
            return None
        return snapshot

    @staticmethod
    def _split_levels(
        levels: Sequence[FundingBookLevel],
    ) -> tuple[
        dict[tuple[float, int], FundingBookLevel], dict[tuple[float, int], FundingBookLevel]
    ]:
        bids: dict[tuple[float, int], FundingBookLevel] = {}
        asks: dict[tuple[float, int], FundingBookLevel] = {}
        for level in levels:
            if level.amount > 0:
                asks[(level.rate, level.period)] = level
            elif level.amount < 0:
                bids[(level.rate, level.period)] = level
        return bids, asks

    @staticmethod
    def _invalidate(state: _BookState) -> None:
        state.requires_reconciliation = True
        state.sequence_valid = False
        state.checksum_valid = False
        state.ws_snapshot_received = False

    @staticmethod
    def _apply_sequence(state: _BookState, sequence: int | None) -> None:
        if sequence is None:
            return
        if state.sequence is None or sequence != state.sequence + 1:
            FundingBookStore._invalidate(state)
        else:
            state.sequence_valid = True
        state.sequence = sequence

    @staticmethod
    def _snapshot_id(symbol: str, state: _BookState) -> str:
        material = (
            f"{symbol}:{state.captured_at_ms}:{state.sequence}:"
            f"{state.source}:"
            f"{[(level.rate, level.period, level.amount) for level in state.bids.values()]}:"
            f"{[(level.rate, level.period, level.amount) for level in state.asks.values()]}"
        )
        return hashlib.sha256(material.encode()).hexdigest()


class _RestFundingBook(Protocol):
    async def get_funding_book(self, *, symbol: str, length: int) -> list[FundingBookLevel]: ...


class _FundingBookWS(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class FundingBookService(FundingBookProvider):
    """Own the WS/REST lifecycle while exposing only the canonical store."""

    def __init__(
        self,
        *,
        store: FundingBookStore,
        rest: _RestFundingBook,
        ws: _FundingBookWS,
        symbols: Sequence[str],
        length: int = 25,
        reconcile_interval_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._rest = rest
        self._ws = ws
        self._symbols = tuple(symbols)
        self._length = length
        self._reconcile_interval_seconds = reconcile_interval_seconds
        set_handlers = getattr(ws, "set_handlers", None)
        if callable(set_handlers):
            set_handlers(
                on_snapshot=self._on_snapshot,
                on_update=self._on_update,
                on_checksum=self._on_checksum,
                on_sequence=self._store.apply_sequence,
                on_disconnect=self._store.mark_disconnected,
            )

    async def start(self) -> None:
        await self._ws.start()

    async def stop(self) -> None:
        await self._ws.stop()

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            while not stop_event.is_set():
                await self.start()
                await self.reconcile_once()
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self._reconcile_interval_seconds
                    )
                except TimeoutError:
                    continue
        finally:
            await self.stop()

    async def reconcile_once(self) -> None:
        for symbol in self._symbols:
            generation = self._store.generation(symbol)
            try:
                levels = await self._rest.get_funding_book(symbol=symbol, length=self._length)
            except Exception:
                # A REST error cannot poison an independently fresh WS snapshot.
                log.exception("funding_book_rest_reconcile_failed symbol=%s", symbol)
                continue
            if not self._store.apply_rest_snapshot(symbol, levels, expected_generation=generation):
                log.info("funding_book_rest_reconcile_deferred symbol=%s", symbol)

    def snapshot(self, symbol: str, *, now_ms: int) -> MarketSnapshot | None:
        return self._store.snapshot(symbol, now_ms=now_ms)

    def _on_snapshot(
        self, symbol: str, raw_levels: list[list[object]], sequence: int | None
    ) -> None:
        self._store.apply_snapshot(
            symbol,
            [FundingBookLevel.from_bitfinex(level) for level in raw_levels],
            sequence=sequence,
        )

    def _on_update(self, symbol: str, raw_level: list[object], sequence: int | None) -> None:
        self._store.apply_update(
            symbol, FundingBookLevel.from_bitfinex(raw_level), sequence=sequence
        )

    def _on_checksum(self, symbol: str, checksum: int, sequence: int | None) -> None:
        self._store.apply_checksum(symbol, checksum=checksum, sequence=sequence)
