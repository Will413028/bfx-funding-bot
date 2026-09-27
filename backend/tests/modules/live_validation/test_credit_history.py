"""Venue credit history and funding trades: parse, page, sync.

Ids, timestamps, rates, periods and the 150.76884612 amount are the live
account's (read 2026-09-27); status strings, the expired credit's amount and
the trailing position pair are placeholders.
"""
import json
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # FK targets for create_all
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import (
    BitfinexAuthREST,
    FundingCreditRecord,
    FundingTrade,
    parse_funding_credit_history,
    parse_funding_trades,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.modules.live_validation.credit_history import (
    CREDIT_RESYNC_MS,
    CreditHistorySync,
)
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    INITIAL_LOOKBACK_MS,
    MS_PER_DAY,
)
from bfx_funding_bot.modules.live_validation.tables import FundingCreditHistoryRow, FundingTradeRow

ACCOUNT = UUID("35efed2d-3004-4941-a161-ca025d9c4d53")
# [ID, SYMBOL, SIDE, MTS_CREATE, MTS_UPDATE, AMOUNT, FLAGS, STATUS, RATE_TYPE, _, _,
#  RATE, PERIOD, MTS_OPENING, MTS_LAST_PAYOUT, NOTIFY, HIDDEN, _, RENEW, _, NO_CLOSE, PAIR]
REPAID_EARLY = [466642176, "fUST", 1, 1790350246000, 1790350246000, 150.76884612, 0,
                "CLOSED (reduced)", "FIXED", None, None, 0.00019999, 2, 1790350246000,
                1790351088000, 0, 0, None, 0, None, 0, "tBTCUST"]
EXPIRED = [466451710, "fUST", 1, 1790100510000, 1790273311000, 150.0, 0, "CLOSED (expired)",
           "FIXED", None, None, 0.0001482, 2, 1790100510000, 1790273311000, 0, 0, None, 0,
           None, 0, "tETHUST"]
TRADE = [432914136, "fUST", 1790350246000, 5123273052, 150.76884612, 0.00019999, 2, None]


def test_parse_credit_history_reads_rate_period_and_actual_close() -> None:
    [early, expired] = parse_funding_credit_history(json.loads(json.dumps([REPAID_EARLY, EXPIRED]),
                                                               parse_float=Decimal))
    assert early.rate == Decimal("0.00019999") and early.period_days == 2
    assert early.amount == Decimal("150.76884612")
    assert early.mts_opening == 1790350246000
    assert early.mts_last_payout - early.mts_opening == 842_000   # 14 min, not 0 days
    assert early.mts_update == early.mts_create                    # why MTS_UPDATE misleads
    assert expired.rate == Decimal("0.0001482")
    assert expired.mts_last_payout - expired.mts_opening == 2 * MS_PER_DAY + 1_000


def test_parse_funding_trade_links_offer() -> None:
    [trade] = parse_funding_trades(json.loads(json.dumps([TRADE]), parse_float=Decimal))
    assert trade == FundingTrade(432914136, "fUST", 1790350246000, 5123273052,
                                 Decimal("150.76884612"), Decimal("0.00019999"), 2, None)


@pytest.mark.parametrize("raw", [{}, [[1, "fUST"]], [[*REPAID_EARLY[:11], None, 2, 1, 1]]])
def test_parse_credit_history_rejects_malformed_rows(raw: object) -> None:
    with pytest.raises(BitfinexShapeError):
        parse_funding_credit_history(raw)


@pytest.mark.parametrize("raw", [{}, [[1, "fUST", 1, 2]], [[1, "fUST", 1, None, 1, 1, 2]]])
def test_parse_trades_rejects_malformed_rows(raw: object) -> None:
    with pytest.raises(BitfinexShapeError):
        parse_funding_trades(raw)


class _Ctx:
    class credentials:  # noqa: N801
        api_key = "KEY"
        api_secret = "SECRET"


@pytest.mark.asyncio
async def test_rest_pages_credit_and_loan_history_backwards_by_update_time() -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    pages = [[REPAID_EARLY], [EXPIRED], [], []]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=pages[len(requests) - 1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rest = BitfinexAuthREST(http=http, nonce_provider=iter(range(1, 100)).__next__)
        got = await rest.get_funding_credit_history(
            ctx=_Ctx(), symbol="fUST", start_ms=0, end_ms=1790500000000, limit=1,  # type: ignore[arg-type]
        )
        loans = await rest.get_funding_credit_history(
            ctx=_Ctx(), symbol="fUST", start_ms=0, end_ms=1790500000000, kind="loan",  # type: ignore[arg-type]
        )
    assert [c.credit_id for c in got] == [466451710, 466642176]
    assert loans == []
    assert requests[0] == ("/v2/auth/r/funding/credits/fUST/hist",
                           {"start": 0, "end": 1790500000000, "limit": 1})
    assert requests[1][1]["end"] == 1790350246000 - 1
    assert requests[3][0] == "/v2/auth/r/funding/loans/fUST/hist"


@pytest.mark.asyncio
async def test_rest_reads_funding_trades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/auth/r/funding/trades/fUST/hist"
        return httpx.Response(200, json=[TRADE])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rest = BitfinexAuthREST(http=http, nonce_provider=iter(range(1, 100)).__next__)
        [trade] = await rest.get_funding_trades(
            ctx=_Ctx(), symbol="fUST", start_ms=0, end_ms=1790500000000,  # type: ignore[arg-type]
        )
    assert trade.offer_id == 5123273052


@pytest_asyncio.fixture
async def sf(sqlite_engine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


class _FakeRest:
    def __init__(self, credits: list[FundingCreditRecord], loans: list[FundingCreditRecord],
                 trades: list[FundingTrade], fail: bool = False) -> None:
        self.credits, self.loans, self.trades, self.fail = credits, loans, trades, fail
        self.windows: list[tuple[str, int, int]] = []

    async def get_funding_credit_history(self, *, ctx, symbol, start_ms, end_ms, kind):  # type: ignore[no-untyped-def]
        self.windows.append((kind, start_ms, end_ms))
        if self.fail:
            raise RuntimeError("venue down")
        return self.credits if kind == "credit" else self.loans

    async def get_funding_trades(self, *, ctx, symbol, start_ms, end_ms):  # type: ignore[no-untyped-def]
        self.windows.append(("trade", start_ms, end_ms))
        return self.trades


def _sync(rest: _FakeRest, sf: async_sessionmaker, now: int) -> CreditHistorySync:
    return CreditHistorySync(
        rest=rest, ctx=_Ctx(), session_factory=sf,  # type: ignore[arg-type]
        exchange_account_id=ACCOUNT, deployment_environment="live", symbols=["fUST"],
        clock=lambda: now,
    )


@pytest.mark.asyncio
async def test_sync_is_idempotent_keeps_kinds_apart_and_rereads_a_full_term(sf) -> None:
    now = 1790500000000
    [early] = parse_funding_credit_history([REPAID_EARLY])
    # A loan may share a credit's numeric id: separate venue sequences.
    [loan] = parse_funding_credit_history([[466642176, *EXPIRED[1:]]])
    rest = _FakeRest([early], [loan], parse_funding_trades([TRADE]))
    sync = _sync(rest, sf, now)
    await sync.tick()
    await sync.tick()
    async with sf() as session:
        credits = (await session.scalars(select(FundingCreditHistoryRow))).all()
        trades = (await session.scalars(select(FundingTradeRow))).all()
    assert sorted((c.kind, c.credit_id) for c in credits) == [("credit", 466642176),
                                                              ("loan", 466642176)]
    assert [t.offer_id for t in trades] == [5123273052]
    first, second = rest.windows[:3], rest.windows[3:]
    assert first == [("credit", now - INITIAL_LOOKBACK_MS, now),
                     ("loan", now - INITIAL_LOOKBACK_MS, now),
                     ("trade", now - INITIAL_LOOKBACK_MS, now)]
    # An ended credit is listed by MTS_UPDATE, which may be as old as its
    # creation, so every sync re-reads one maximum term.
    assert second == [("credit", now - CREDIT_RESYNC_MS, now), ("loan", now - CREDIT_RESYNC_MS, now),
                      ("trade", 1790350246000 - MS_PER_DAY, now)]


@pytest.mark.asyncio
async def test_sync_waits_for_the_final_payout_before_storing_a_credit(sf) -> None:
    [unsettled] = parse_funding_credit_history([[*REPAID_EARLY[:14], None, *REPAID_EARLY[15:]]])
    await _sync(_FakeRest([unsettled], [], []), sf, 1790500000000).tick()
    async with sf() as session:
        assert (await session.scalars(select(FundingCreditHistoryRow))).all() == []


@pytest.mark.asyncio
async def test_sync_failure_is_fail_open(sf) -> None:
    await _sync(_FakeRest([], [], [], fail=True), sf, 1790500000000).tick()
