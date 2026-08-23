from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

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


def test_sequence_gap_requires_ws_snapshot_then_rest_reconciliation() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)
    store.apply_sequence("fUST", 12)

    assert store.snapshot("fUST", now_ms=1_000) is None

    store.apply_rest_snapshot("fUST", _book_snapshot())
    assert store.snapshot("fUST", now_ms=1_000) is None

    store.apply_snapshot("fUST", _book_snapshot(), sequence=20)
    store.apply_rest_snapshot("fUST", _book_snapshot())

    snapshot = store.snapshot("fUST", now_ms=1_000)
    assert snapshot is not None
    assert snapshot.source == "rest_reconciled"
    assert snapshot.sequence_valid and snapshot.checksum_valid


def test_disconnect_makes_previous_snapshot_unusable() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)

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
async def test_service_uses_one_store_provider_and_periodically_reconciles_rest() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)
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
    assert rest.calls == [("fUST", 25)]
    assert snapshot is not None
    assert snapshot.source == "rest_reconciled"


@pytest.mark.asyncio
async def test_rest_reconciliation_error_keeps_a_fresh_ws_snapshot_usable() -> None:
    store = FundingBookStore(max_age_seconds=30, clock=lambda: 1_000)
    store.apply_snapshot("fUST", _book_snapshot(), sequence=10)
    service = FundingBookService(
        store=store,
        rest=_FakeRest(RuntimeError("rest unavailable")),
        ws=_FakeWS(),
        symbols=("fUST",),
    )

    await service.reconcile_once()

    snapshot = service.snapshot("fUST", now_ms=1_000)
    assert snapshot is not None
    assert snapshot.source == "ws"


@pytest.mark.asyncio
async def test_run_restarts_the_ws_client_after_each_reconcile_interval() -> None:
    stop_event = asyncio.Event()

    class _StoppingRest(_FakeRest):
        async def get_funding_book(self, *, symbol: str, length: int) -> list[FundingBookLevel]:
            levels = await super().get_funding_book(symbol=symbol, length=length)
            if len(self.calls) == 2:
                stop_event.set()
            return levels

    ws = _CountingWS()
    service = FundingBookService(
        store=FundingBookStore(max_age_seconds=30, clock=lambda: 1_000),
        rest=_StoppingRest(),
        ws=ws,
        symbols=("fUST",),
        reconcile_interval_seconds=0.001,
    )

    await service.run(stop_event)

    assert ws.start_calls == 2
    assert ws.stopped
