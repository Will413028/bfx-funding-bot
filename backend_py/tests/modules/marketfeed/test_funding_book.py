from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.funding_book_ws import FundingBookWSClient
from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel
from bfx_funding_bot.modules.marketfeed.funding_book import (
    FundingBookService,
    FundingBookStore,
    MarketSnapshot,
)


def _level(*, rate: str, period: int, amount: str) -> FundingBookLevel:
    return FundingBookLevel(rate=float(rate), period=period, count=1, amount=float(amount))


def _book_snapshot() -> list[FundingBookLevel]:
    return [
        _level(rate="0.00020", period=7, amount="-100"),
        _level(rate="0.00021", period=7, amount="100"),
    ]


def _valid_ws_store() -> FundingBookStore:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)
    store.apply_sequence("fUST", 11)
    store.apply_checksum("fUST", checksum=123, expected=123, sequence=12)
    return store


def _snapshot(
    *,
    bids: list[FundingBookLevel],
    asks: list[FundingBookLevel],
    captured_at_ms: int = 1_000,
    sequence_valid: bool = True,
    checksum_valid: bool = True,
) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id="book-1",
        symbol="fUST",
        bids=tuple(bids),
        asks=tuple(asks),
        captured_at_ms=captured_at_ms,
        received_at_ms=captured_at_ms,
        source="ws",
        sequence_valid=sequence_valid,
        checksum_valid=checksum_valid,
        sequence=42,
    )


def test_invalid_checksum_makes_snapshot_unusable() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    store.apply_snapshot("fUST", _book_snapshot())
    store.apply_checksum("fUST", checksum=123, expected=456)

    assert store.snapshot("fUST", now_ms=1_000) is None


def test_ws_snapshot_is_priceable_as_soon_as_the_venue_hands_the_book_over() -> None:
    """A whole book from the venue is the evidence; `cs` audits it afterwards.

    The venue sends checksums on its own schedule -- tens of seconds apart,
    sometimes over two minutes. Withholding the book until one arrives leaves
    it ineligible for most of its life with nothing ever wrong with it.
    """
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)

    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)

    assert store.snapshot("fUST", now_ms=1_000) is not None


def test_valid_sequence_and_checksum_make_ws_snapshot_available() -> None:
    store = _valid_ws_store()

    assert store.snapshot("fUST", now_ms=1_000) is not None


def test_reconnect_snapshot_restores_the_book_without_a_rest_rebase() -> None:
    store = _valid_ws_store()
    store.mark_disconnected()

    assert store.snapshot("fUST", now_ms=1_000) is None

    # Resubscribing is the only thing that makes the venue resend a snapshot,
    # and what it resends is a whole book again.
    store.apply_snapshot("fUST", _book_snapshot(), sequence=20)

    snapshot = store.snapshot("fUST", now_ms=1_000)
    assert snapshot is not None
    assert snapshot.source == "ws"


def test_interleaved_symbols_share_one_connection_sequence() -> None:
    """Bitfinex numbers public frames per connection, not per channel.

    With fUSD and fUST on one socket the numbers alternate between them, so
    checking for +1 within a symbol reads every second frame as a gap and
    leaves both books permanently dead.
    """
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)

    store.apply_snapshot("fUSD", _book_snapshot(), sequence=1)
    store.apply_snapshot("fUST", _book_snapshot(), sequence=2)
    store.apply_sequence("fUSD", 3)
    store.apply_sequence("fUST", 4)

    assert store.snapshot("fUSD", now_ms=1_000) is not None
    assert store.snapshot("fUST", now_ms=1_000) is not None


def test_a_sequence_gap_voids_every_symbol_on_the_connection() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    store.apply_snapshot("fUSD", _book_snapshot(), sequence=1)
    store.apply_snapshot("fUST", _book_snapshot(), sequence=2)

    store.apply_sequence("fUSD", 7)  # frames 3..6 never arrived

    assert store.snapshot("fUSD", now_ms=1_000) is None
    assert store.snapshot("fUST", now_ms=1_000) is None


def test_heartbeat_keeps_a_quiet_book_fresh() -> None:
    """A funding book can sit unchanged for minutes and still be current.

    The venue says so by heartbeat. Ageing the book by its last content change
    would retire one that is correct, unchanged, and still being confirmed.
    """
    clock = {"now": 1_000}
    store = FundingBookStore(max_age_seconds=30, clock=lambda: clock["now"])
    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)

    assert store.snapshot("fUST", now_ms=41_000) is None

    clock["now"] = 40_000
    store.apply_sequence("fUST", 11)

    assert store.snapshot("fUST", now_ms=41_000) is not None


def test_ws_update_keeps_the_book_priceable() -> None:
    """An increment the venue sent, in sequence, still yields the venue's book.

    Retiring the checksum per increment demands one `cs` frame per increment,
    which the venue never sends.
    """
    store = _valid_ws_store()

    store.apply_update("fUST", _level(rate="0.00022", period=7, amount="100"), sequence=13)

    snapshot = store.snapshot("fUST", now_ms=1_000)
    assert snapshot is not None
    assert any(level.rate == 0.00022 for level in snapshot.asks)


def test_rest_rebase_is_priceable_evidence_of_its_own() -> None:
    """REST returns the same venue's book, whole, over a 200.

    Requiring a *WS* snapshot to re-qualify it would never be satisfiable --
    the venue sends one only on subscribe -- so the book would stay dead for
    as long as the process lived.
    """
    store = _valid_ws_store()

    assert store.apply_rest_snapshot("fUST", _book_snapshot())

    snapshot = store.snapshot("fUST", now_ms=1_000)
    assert snapshot is not None
    assert snapshot.source == "rest_reconciled"


def test_exact_period_requires_depth_and_uses_absolute_bid_amount() -> None:
    snapshot = _snapshot(
        bids=[_level(rate="0.00020", period=7, amount="-100")],
        asks=[_level(rate="0.00021", period=7, amount="100")],
    )

    period = snapshot.exact_period(period_days=7, amount=Decimal("100"))

    assert period is not None
    assert period.bids[0].amount == Decimal("-100")
    assert snapshot.exact_period(period_days=14, amount=Decimal("100")) is None
    assert snapshot.exact_period(period_days=7, amount=Decimal("101")) is None


def test_snapshot_freshness_fails_closed_for_future_invalid_or_wrong_symbol() -> None:
    snapshot = _snapshot(
        bids=[_level(rate="0.00020", period=7, amount="-100")],
        asks=[_level(rate="0.00021", period=7, amount="100")],
        captured_at_ms=1_001,
    )

    assert not snapshot.is_fresh(symbol="fUST", now_ms=1_000, max_age_ms=30_000)
    assert not snapshot.is_fresh(symbol="fUSD", now_ms=2_000, max_age_ms=30_000)
    assert not _snapshot(bids=[], asks=[], sequence_valid=False).is_fresh(
        symbol="fUST", now_ms=1_000, max_age_ms=30_000
    )
    assert not _snapshot(bids=[], asks=[], checksum_valid=False).is_fresh(
        symbol="fUST", now_ms=1_000, max_age_ms=30_000
    )


def test_sequence_gap_voids_the_book_until_a_whole_one_replaces_it() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)
    store.apply_sequence("fUST", 12)  # frame 11 never arrived

    assert store.snapshot("fUST", now_ms=1_000) is None

    assert store.apply_rest_snapshot("fUST", _book_snapshot())

    snapshot = store.snapshot("fUST", now_ms=1_000)
    assert snapshot is not None
    assert snapshot.source == "rest_reconciled"
    assert snapshot.sequence_valid and snapshot.checksum_valid


def test_disconnect_makes_previous_snapshot_unusable() -> None:
    store = _valid_ws_store()

    store.mark_disconnected()

    assert store.snapshot("fUST", now_ms=1_000) is None


class _FakeRest:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._error = error

    async def get_funding_book(self, *, symbol: str, length: int) -> list[FundingBookLevel]:
        self.calls.append((symbol, length))
        if self._error is not None:
            raise self._error
        return _book_snapshot()


class _FakeWS:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _CountingWS(_FakeWS):
    def __init__(self) -> None:
        super().__init__()
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        await super().start()


@pytest.mark.asyncio
async def test_service_serves_a_healthy_book_and_leaves_it_alone() -> None:
    """A periodic rebase over a healthy book buys nothing and costs its evidence."""
    store = _valid_ws_store()
    rest = _FakeRest()
    ws = _FakeWS()
    service = FundingBookService(
        store=store,
        rest=rest,
        ws=ws,
        symbols=("fUST",),
        length=25,
    )

    await service.start()
    await service.reconcile_once()
    snapshot = service.snapshot("fUST", now_ms=1_000)
    await service.stop()

    assert ws.started and ws.stopped
    assert rest.calls == []
    assert snapshot is not None


@pytest.mark.asyncio
async def test_service_rebuilds_a_book_that_has_no_baseline() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    rest = _FakeRest()
    service = FundingBookService(
        store=store,
        rest=rest,
        ws=_FakeWS(),
        symbols=("fUST",),
        length=25,
    )

    await service.reconcile_once()

    assert rest.calls == [("fUST", 25)]
    assert service.snapshot("fUST", now_ms=1_000) is not None


@pytest.mark.asyncio
async def test_rest_reconciliation_error_leaves_the_book_blocked_not_broken() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    service = FundingBookService(
        store=store,
        rest=_FakeRest(RuntimeError("rest unavailable")),
        ws=_FakeWS(),
        symbols=("fUST",),
    )

    await service.reconcile_once()

    assert service.snapshot("fUST", now_ms=1_000) is None

    # The failed rebase poisons nothing: the next whole book from the venue
    # stands on its own.
    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)

    snapshot = service.snapshot("fUST", now_ms=1_000)
    assert snapshot is not None
    assert snapshot.source == "ws"


@pytest.mark.asyncio
async def test_reconciliation_does_not_overwrite_a_book_the_venue_sent_meanwhile() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    started = asyncio.Event()
    release = asyncio.Event()

    class _DeferredRest:
        async def get_funding_book(self, *, symbol: str, length: int) -> list[FundingBookLevel]:
            started.set()
            await release.wait()
            return _book_snapshot()

    service = FundingBookService(
        store=store,
        rest=_DeferredRest(),
        ws=_FakeWS(),
        symbols=("fUST",),
    )

    reconcile = asyncio.create_task(service.reconcile_once())
    await started.wait()
    store.apply_snapshot(
        "fUST",
        [*_book_snapshot(), _level(rate="0.00022", period=7, amount="100")],
        sequence=10,
    )
    release.set()
    await reconcile

    snapshot = service.snapshot("fUST", now_ms=1_000)
    assert snapshot is not None
    assert snapshot.source == "ws"
    assert any(level.rate == 0.00022 for level in snapshot.asks)


@pytest.mark.asyncio
async def test_disconnect_during_first_rest_request_rejects_stale_rebase() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    started = asyncio.Event()
    release = asyncio.Event()

    class _DeferredFirstRest:
        def __init__(self) -> None:
            self.calls = 0

        async def get_funding_book(
            self, *, symbol: str, length: int
        ) -> list[FundingBookLevel]:
            self.calls += 1
            if self.calls == 1:
                started.set()
                await release.wait()
            return _book_snapshot()

    ws = FundingBookWSClient(symbols=("fUST",))
    service = FundingBookService(
        store=store,
        rest=_DeferredFirstRest(),
        ws=ws,
        symbols=("fUST",),
    )

    reconcile = asyncio.create_task(service.reconcile_once())
    await started.wait()
    ws.handle_raw('{"event":"error","msg":"stream failed"}')
    release.set()
    await reconcile

    # The rebase was fetched before the disconnect, so it describes a book from
    # the other side of a gap and must not be adopted.
    assert service.snapshot("fUST", now_ms=1_000) is None

    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)

    assert service.snapshot("fUST", now_ms=1_000) is not None


def test_ws_error_event_invalidates_the_canonical_store() -> None:
    store = _valid_ws_store()
    client = FundingBookWSClient(symbols=("fUST",), on_disconnect=store.mark_disconnected)

    client.handle_raw('{"event":"error","msg":"stream failed"}')

    assert store.snapshot("fUST", now_ms=1_000) is None


def test_ws_error_voids_the_book_until_the_venue_hands_over_a_whole_one() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    client = FundingBookWSClient(symbols=("fUST",), on_disconnect=store.mark_disconnected)

    client.handle_raw('{"event":"error","msg":"stream failed"}')

    assert store.snapshot("fUST", now_ms=1_000) is None

    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)

    assert store.snapshot("fUST", now_ms=1_000) is not None


@pytest.mark.asyncio
async def test_run_restarts_the_ws_client_after_each_reconcile_interval() -> None:
    stop_event = asyncio.Event()

    class _StoppingWS(_CountingWS):
        async def start(self) -> None:
            await super().start()
            if self.start_calls == 2:
                stop_event.set()

    ws = _StoppingWS()
    service = FundingBookService(
        store=FundingBookStore(max_age_seconds=30, clock=lambda: 1_000),
        rest=_FakeRest(),
        ws=ws,
        symbols=("fUST",),
        reconcile_interval_seconds=0.001,
    )

    await service.run(stop_event)

    assert ws.start_calls == 2
    assert ws.stopped
