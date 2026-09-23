"""Canonical, fail-closed live funding-book snapshot provider."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
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
    # Both assert the absence of a detected inconsistency, not the presence of
    # a proof. A book the venue handed over whole is consistent until the venue
    # says otherwise: it numbers every frame, so a gap is visible, and it sends
    # a checksum on its own schedule -- tens of seconds apart, sometimes over
    # two minutes. Waiting for that proof before pricing would idle the book
    # for most of its life while nothing was ever wrong with it.
    sequence_valid: bool
    checksum_valid: bool
    sequence: int | None
    # Bound by the provider that admitted this exact snapshot, never a fresh
    # unrelated book substituted after pricing. Missing evidence cannot send.
    max_age_ms: int | None = None

    def is_fresh(self, *, symbol: str, now_ms: int, max_age_ms: int) -> bool:
        """Age the book by when the venue last confirmed it, not by its content.

        A quiet funding book can go minutes without a level changing while the
        venue keeps confirming, per heartbeat, that nothing has. Measuring from
        `captured_at_ms` would retire such a book for being correct and
        unchanged; `received_at_ms` measures the thing that actually decays --
        how long since the venue last spoke to us about it.
        """
        return (
            symbol == self.symbol
            # Neither timestamp may sit in the future: that is a broken clock,
            # not a fresh book.
            and self.captured_at_ms <= now_ms
            and self.received_at_ms <= now_ms
            and now_ms - self.received_at_ms <= max_age_ms
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


class BookUnavailable(StrEnum):
    """Why a book cannot price right now, kept distinct for the operator.

    Collapsing these into one reason is what made the venue-checksum fault take
    a full investigation: the block said the book was stale when in truth it had
    never once qualified, and nothing in the event said which.
    """

    NO_BASELINE = "no_baseline"
    SEQUENCE_GAP = "sequence_gap"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    STALE = "stale"


class FundingBookProvider(Protocol):
    def snapshot(self, symbol: str, *, now_ms: int) -> MarketSnapshot | None: ...

    def unavailable_reason(self, symbol: str, *, now_ms: int) -> BookUnavailable | None: ...


@dataclass(slots=True)
class _BookState:
    bids: dict[tuple[float, int], FundingBookLevel] = field(default_factory=dict)
    asks: dict[tuple[float, int], FundingBookLevel] = field(default_factory=dict)
    captured_at_ms: int | None = None
    received_at_ms: int | None = None
    generation: int = 0
    # A full book to apply increments onto: a WS snapshot or a REST rebase.
    has_baseline: bool = False
    # Inconsistencies observed since that baseline was established.
    sequence_gap: bool = False
    checksum_mismatch: bool = False
    # Observability only: when the venue last positively confirmed this book.
    checksum_verified_at_ms: int | None = None
    requires_reconciliation: bool = False
    reconciliation_epoch: int = 0
    source: Literal["ws", "rest_reconciled"] = "ws"


class FundingBookStore(FundingBookProvider):
    """Single in-memory source for live eligibility snapshots."""

    def __init__(self, *, max_age_seconds: float, clock: Callable[[], int] | None = None) -> None:
        self._max_age_ms = int(max_age_seconds * 1_000)
        self._clock = clock or (lambda: int(time.time() * 1_000))
        self._states: dict[str, _BookState] = {}
        self._reconciliation_epoch = 0
        # Bitfinex numbers public frames once per connection, across every
        # subscribed channel, so continuity is a connection-wide fact: a gap
        # means this connection dropped frames and no symbol can be trusted.
        self._connection_sequence: int | None = None

    def apply_snapshot(
        self,
        symbol: str,
        levels: Sequence[FundingBookLevel],
        *,
        sequence: int | None = None,
    ) -> None:
        state = self._state_for(symbol)
        state.bids, state.asks = self._split_levels(levels)
        state.captured_at_ms = self._clock()
        state.received_at_ms = self._clock()
        state.source = "ws"
        # Settle the connection's continuity first: a gap here voids the other
        # symbols' books. This one is exempt -- it is a whole book, correct
        # whatever the connection missed before it arrived. The venue resends
        # it only on resubscribe, which is the sole way a book is rebuilt over
        # WS, so nothing else could re-qualify it.
        self._observe_sequence(sequence)
        self._establish_baseline(state)
        state.generation += 1

    def apply_update(
        self, symbol: str, level: FundingBookLevel, *, sequence: int | None = None
    ) -> None:
        state = self._state_for(symbol)
        self._observe_sequence(sequence)
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
        # The increment is authoritative and arrived in sequence, so the book
        # it produces is still the venue's. Retiring the book here would demand
        # a fresh `cs` frame per increment; the venue sends those tens of
        # seconds apart, so the book would be ineligible almost always. A
        # checksum that disagrees still voids it -- see apply_checksum.
        state.generation += 1

    def apply_sequence(self, symbol: str, sequence: int) -> None:
        state = self._state_for(symbol)
        self._observe_sequence(sequence)
        # A heartbeat changes no level but does say the book is still current.
        state.received_at_ms = self._clock()
        state.generation += 1

    def apply_checksum(
        self,
        symbol: str,
        *,
        checksum: int,
        expected: int | None = None,
        sequence: int | None = None,
    ) -> None:
        state = self._state_for(symbol)
        self._observe_sequence(sequence)
        computed = (
            expected
            if expected is not None
            else funding_book_checksum(
                bids=list(state.bids.values()), asks=list(state.asks.values())
            )
        )
        if checksum != computed:
            state.checksum_mismatch = True
            self._invalidate(state)
        elif state.has_baseline:
            state.checksum_verified_at_ms = self._clock()
            state.received_at_ms = self._clock()
        state.generation += 1

    def apply_rest_snapshot(
        self,
        symbol: str,
        levels: Sequence[FundingBookLevel],
        *,
        expected_generation: int | None = None,
        expected_reconciliation_epoch: int | None = None,
    ) -> bool:
        if (
            expected_reconciliation_epoch is not None
            and self._reconciliation_epoch != expected_reconciliation_epoch
        ):
            return False
        state = self._state_for(symbol)
        if expected_generation is not None and state.generation != expected_generation:
            return False
        state.bids, state.asks = self._split_levels(levels)
        state.captured_at_ms = self._clock()
        state.received_at_ms = self._clock()
        state.source = "rest_reconciled"
        # REST returns the same venue's book, whole, over an HTTP 200.
        # It is a baseline on the same footing as a WS snapshot: later WS
        # increments apply onto it, and it is priceable within the freshness
        # bound. Demanding a *WS* snapshot to re-qualify it would never be
        # satisfiable -- the venue sends one only on subscribe -- so the book
        # would stay dead for as long as the process lived.
        self._establish_baseline(state)
        state.generation += 1
        return True

    def mark_disconnected(self) -> None:
        self._reconciliation_epoch += 1
        self._connection_sequence = None
        for state in self._states.values():
            self._invalidate(state)
            state.generation += 1

    def needs_reconciliation(self, symbol: str) -> bool:
        """Whether this symbol has no trustworthy book to apply increments to."""
        state = self._states.get(symbol)
        if state is None:
            return True
        return (
            state.requires_reconciliation
            or not state.has_baseline
            or state.reconciliation_epoch != self._reconciliation_epoch
        )

    def generation(self, symbol: str) -> int:
        state = self._states.get(symbol)
        return state.generation if state is not None else 0

    def reconciliation_epoch(self) -> int:
        return self._reconciliation_epoch

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
            sequence_valid=not state.sequence_gap,
            checksum_valid=not state.checksum_mismatch,
            sequence=self._connection_sequence,
            max_age_ms=self._max_age_ms,
        )
        if (
            state.requires_reconciliation
            or not state.has_baseline
            or state.reconciliation_epoch != self._reconciliation_epoch
            or not snapshot.is_fresh(symbol=symbol, now_ms=now_ms, max_age_ms=self._max_age_ms)
        ):
            return None
        return snapshot

    def unavailable_reason(self, symbol: str, *, now_ms: int) -> BookUnavailable | None:
        """Name what is withholding this book, or None when it is priceable."""
        state = self._states.get(symbol)
        if state is None or state.captured_at_ms is None or state.received_at_ms is None:
            return BookUnavailable.NO_BASELINE
        # A detected inconsistency outranks the missing baseline it caused.
        if state.checksum_mismatch:
            return BookUnavailable.CHECKSUM_MISMATCH
        if state.sequence_gap:
            return BookUnavailable.SEQUENCE_GAP
        if self.needs_reconciliation(symbol):
            return BookUnavailable.NO_BASELINE
        if self.snapshot(symbol, now_ms=now_ms) is None:
            return BookUnavailable.STALE
        return None

    def _establish_baseline(self, state: _BookState) -> None:
        """Adopt a whole book from the venue and clear what it supersedes."""
        state.has_baseline = True
        state.sequence_gap = False
        state.checksum_mismatch = False
        state.checksum_verified_at_ms = None
        state.requires_reconciliation = False
        state.reconciliation_epoch = self._reconciliation_epoch

    def _state_for(self, symbol: str) -> _BookState:
        state = self._states.get(symbol)
        if state is None:
            state = _BookState(requires_reconciliation=self._reconciliation_epoch > 0)
            self._states[symbol] = state
        return state

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
        state.checksum_verified_at_ms = None
        state.has_baseline = False

    def _observe_sequence(self, sequence: int | None) -> None:
        """Track connection-wide frame continuity; a gap voids every book."""
        if sequence is None:
            return
        previous = self._connection_sequence
        self._connection_sequence = sequence
        if previous is None or sequence == previous + 1:
            # First numbered frame on this connection, or an unbroken run.
            return
        for state in self._states.values():
            state.sequence_gap = True
            self._invalidate(state)

    def _snapshot_id(self, symbol: str, state: _BookState) -> str:
        material = (
            f"{symbol}:{state.captured_at_ms}:{self._connection_sequence}:"
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
        """Rebuild only the books that have no trustworthy baseline left.

        A periodic rebase over a healthy book would retire the sequence and
        checksum evidence the venue just confirmed, and the next confirmation
        arrives only with the next ``cs`` frame -- so the book would spend most
        of its life ineligible for no gain.
        """
        for symbol in self._symbols:
            if not self._store.needs_reconciliation(symbol):
                continue
            generation = self._store.generation(symbol)
            reconciliation_epoch = self._store.reconciliation_epoch()
            try:
                levels = await self._rest.get_funding_book(symbol=symbol, length=self._length)
            except Exception:
                # A REST error cannot poison an independently fresh WS snapshot.
                log.exception("funding_book_rest_reconcile_failed symbol=%s", symbol)
                continue
            if not self._store.apply_rest_snapshot(
                symbol,
                levels,
                expected_generation=generation,
                expected_reconciliation_epoch=reconciliation_epoch,
            ):
                log.info("funding_book_rest_reconcile_deferred symbol=%s", symbol)

    def snapshot(self, symbol: str, *, now_ms: int) -> MarketSnapshot | None:
        return self._store.snapshot(symbol, now_ms=now_ms)

    def unavailable_reason(self, symbol: str, *, now_ms: int) -> BookUnavailable | None:
        return self._store.unavailable_reason(symbol, now_ms=now_ms)

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
