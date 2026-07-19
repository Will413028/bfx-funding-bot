"""BookSnapshotWriter — periodic funding-book depth recording (self-collected
history for future book-aware backtests; Bitfinex serves no historical book).

Additive observability: every failure path is fail-open (log + skip tick) —
the daemon must never die because a snapshot fetch/write hiccuped.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.marketfeed.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel
from bfx_funding_bot.modules.marketfeed.book_snapshot import BookSnapshotWriter
from bfx_funding_bot.modules.marketfeed.tables import FundingBookSnapshotRow

_BOOK = [
    FundingBookLevel(rate=0.00021, period=30, count=3, amount=120_000.0),   # ask
    FundingBookLevel(rate=0.00022, period=2, count=1, amount=50_000.0),     # ask
    FundingBookLevel(rate=0.00018, period=2, count=2, amount=-80_000.0),    # bid
    FundingBookLevel(rate=0.00017, period=30, count=1, amount=-20_000.0),   # bid
]


@pytest_asyncio.fixture
async def sf(sqlite_engine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


class _FakeRest:
    def __init__(self, books: dict[str, list[FundingBookLevel]] | Exception):
        self._books = books
        self.calls: list[str] = []

    async def get_funding_book(self, *, symbol: str, length: int = 25):
        self.calls.append(symbol)
        if isinstance(self._books, Exception):
            raise self._books
        return self._books[symbol]


@pytest.mark.asyncio
async def test_tick_writes_snapshot_with_side_split_and_derived(sf) -> None:
    rest = _FakeRest({"fUST": _BOOK})
    writer = BookSnapshotWriter(
        rest=rest, session_factory=sf, symbols=("fUST",),
        interval_s=3600, clock=lambda: 1_700_000_000_000,
    )
    await writer.tick()

    async with sf() as s:
        row = (await s.execute(select(FundingBookSnapshotRow))).scalar_one()
    assert row.symbol == "fUST"
    assert row.captured_at_ms == 1_700_000_000_000
    assert row.best_ask_rate == Decimal("0.00021")   # lowest lender offer
    assert row.best_bid_rate == Decimal("0.00018")   # highest borrower bid
    assert row.ask_depth == Decimal("170000")
    assert row.bid_depth == Decimal("100000")        # |amounts| summed
    assert len(row.payload["asks"]) == 2
    assert len(row.payload["bids"]) == 2
    assert row.payload["bids"][0][3] > 0             # amounts stored positive


@pytest.mark.asyncio
async def test_tick_covers_all_symbols(sf) -> None:
    rest = _FakeRest({"fUST": _BOOK, "fUSD": []})
    writer = BookSnapshotWriter(
        rest=rest, session_factory=sf, symbols=("fUST", "fUSD"),
        interval_s=3600, clock=lambda: 1000,
    )
    await writer.tick()
    assert rest.calls == ["fUST", "fUSD"]
    async with sf() as s:
        rows = (await s.execute(select(FundingBookSnapshotRow))).scalars().all()
    # empty book still snapshots (that IS information: no liquidity)
    assert {r.symbol for r in rows} == {"fUST", "fUSD"}
    fusd = next(r for r in rows if r.symbol == "fUSD")
    assert fusd.best_ask_rate is None and fusd.bid_depth == Decimal("0")


@pytest.mark.asyncio
async def test_tick_fail_open_on_fetch_error(sf) -> None:
    writer = BookSnapshotWriter(
        rest=_FakeRest(RuntimeError("venue down")), session_factory=sf,
        symbols=("fUST",), interval_s=3600, clock=lambda: 1000,
    )
    await writer.tick()  # must not raise
    async with sf() as s:
        rows = (await s.execute(select(FundingBookSnapshotRow))).scalars().all()
    assert rows == []
