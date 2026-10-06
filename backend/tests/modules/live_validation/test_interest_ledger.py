"""Realized interest from the venue ledger: parse, page, sync, summarize.

Numbers are the live account's payouts of 2026-09-23..27 (read 2026-09-27).
"""
import json
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

# Every table the shared metadata may reach by foreign key, whatever was imported first.
import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.uncertainty_tables
import bfx_funding_bot.modules.ledger.tables
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import (
    BitfinexAuthREST,
    InterestPayment,
    parse_interest_payments,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    MS_PER_DAY,
    InterestLedgerSync,
    funding_currency,
    summarize_interest,
)
from bfx_funding_bot.modules.live_validation.tables import FundingInterestPaymentRow

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
DESC = "Margin Funding Payment on wallet funding"
# [ID, CURRENCY, WALLET, MTS, _, AMOUNT, BALANCE, _, DESCRIPTION], newest first.
LIVE_ROWS = [
    [10578187002, "UST", "funding", 1790472624000, None, 0.0518895, 395.56843927, None, DESC],
    [10577034846, "UST", "funding", 1790386231000, None, 0.00213578, 395.51654977, None, DESC],
    [10575117152, "UST", "funding", 1790299821000, None, 0.01948396, 395.51441399, None, DESC],
]


def _payment(ledger_id: int, mts: int, amount: str, balance: str) -> InterestPayment:
    return InterestPayment(ledger_id, "UST", "funding", mts, Decimal(amount), Decimal(balance), DESC)


def test_parse_live_ledger_rows() -> None:
    parsed = parse_interest_payments(json.loads(json.dumps(LIVE_ROWS)))
    assert [p.ledger_id for p in parsed] == [10578187002, 10577034846, 10575117152]
    assert parsed[0].amount == Decimal("0.0518895")
    assert parsed[0].balance == Decimal("395.56843927")
    assert parsed[0].wallet == "funding" and parsed[0].description == DESC


@pytest.mark.parametrize("raw", [{}, [[1, "UST"]], [[1, "UST", "funding", None, None, 1, 1, None, "x"]]])
def test_parse_rejects_malformed_rows(raw: object) -> None:
    with pytest.raises(BitfinexShapeError):
        parse_interest_payments(raw)


class _Ctx:
    class credentials:  # noqa: N801
        api_key = "KEY"
        api_secret = "SECRET"


@pytest.mark.asyncio
async def test_rest_pages_backwards_and_returns_oldest_first() -> None:
    bodies: list[dict[str, object]] = []
    pages = [LIVE_ROWS[:2], LIVE_ROWS[2:]]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/auth/r/ledgers/UST/hist"
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=pages[len(bodies) - 1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rest = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(iter(range(1, 100)).__next__))
        got = await rest.get_interest_payments(
            ctx=_Ctx(), currency="UST", start_ms=0, end_ms=1790500000000, limit=2,  # type: ignore[arg-type]
        )
    assert [p.ledger_id for p in got] == [10575117152, 10577034846, 10578187002]
    assert bodies[0] == {"category": 28, "start": 0, "end": 1790500000000, "limit": 2}
    assert bodies[1]["end"] == 1790386231000 - 1


@pytest_asyncio.fixture
async def sf(sqlite_engine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


class _FakeRest:
    def __init__(self, result: list[InterestPayment] | Exception) -> None:
        self.result = result
        self.windows: list[tuple[int, int]] = []

    async def get_interest_payments(self, *, ctx, currency, start_ms, end_ms):  # type: ignore[no-untyped-def]
        self.windows.append((start_ms, end_ms))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _sync(rest: _FakeRest, sf: async_sessionmaker, now: int) -> InterestLedgerSync:
    return InterestLedgerSync(
        rest=rest, ctx=_Ctx(), session_factory=sf,  # type: ignore[arg-type]
        exchange_account_id=ACCOUNT, deployment_environment="live",
        currencies=["UST"], clock=lambda: now,
    )


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_resumes_from_latest_with_overlap(sf) -> None:
    now = 1790500000000
    rest = _FakeRest(parse_interest_payments(LIVE_ROWS))
    sync = _sync(rest, sf, now)
    await sync.tick()
    await sync.tick()
    async with sf() as session:
        rows = (await session.scalars(select(FundingInterestPaymentRow))).all()
    assert sorted(r.ledger_id for r in rows) == [10575117152, 10577034846, 10578187002]
    assert {r.deployment_environment for r in rows} == {"live"}
    assert rest.windows[0] == (now - 365 * MS_PER_DAY, now)
    assert rest.windows[1] == (1790472624000 - MS_PER_DAY, now)


@pytest.mark.asyncio
async def test_sync_failure_is_fail_open(sf) -> None:
    await _sync(_FakeRest(RuntimeError("venue down")), sf, 1790500000000).tick()


def test_summary_is_net_interest_over_mean_wallet_balance() -> None:
    week = 7 * MS_PER_DAY
    payments = [_payment(i, 1_000 + i * MS_PER_DAY, "0.05", "400.05") for i in range(7)]
    s = summarize_interest(payments, currency="UST", start_ms=0, end_ms=week)
    assert s.payouts == 7
    assert s.net_interest == Decimal("0.35")
    assert s.mean_balance == Decimal("400")
    # 0.35 / 400 / 7 days * 365 = 4.5625%
    assert s.net_apr_pct == Decimal("4.5625")


def test_summary_without_payouts_has_no_apr() -> None:
    s = summarize_interest([], currency="UST", start_ms=0, end_ms=MS_PER_DAY)
    assert (s.payouts, s.net_interest, s.mean_balance, s.net_apr_pct) == (0, Decimal("0"), None, None)


def test_week_without_payouts_is_zero_when_the_wallet_is_known() -> None:
    """A later week with no payout earned 0 on a known wallet; it is not "no data"."""
    earlier = [_payment(1, 1_000, "0.05", "400.05"), _payment(2, 2_000, "0.01", "401")]
    s = summarize_interest(earlier, currency="UST", start_ms=7 * MS_PER_DAY, end_ms=14 * MS_PER_DAY)
    assert (s.payouts, s.net_interest, s.mean_balance, s.net_apr_pct) == (
        0, Decimal("0"), Decimal("401"), Decimal("0"))
    # a payout only after the window says nothing about the wallet during it
    later = summarize_interest(earlier, currency="UST", start_ms=0, end_ms=500)
    assert (later.mean_balance, later.net_apr_pct) == (None, None)


def test_funding_currency() -> None:
    assert funding_currency("fUST") == "UST"
    with pytest.raises(ValueError):
        funding_currency("UST")


@pytest.mark.asyncio
async def test_sync_keeps_only_funding_wallet_payouts(sf) -> None:
    margin = InterestPayment(1, "UST", "margin", 1790472624000, Decimal("-0.01"), Decimal("5"), "Margin interest")
    await _sync(_FakeRest([margin, *parse_interest_payments(LIVE_ROWS[:1])]), sf, 1790500000000).tick()
    async with sf() as session:
        rows = (await session.scalars(select(FundingInterestPaymentRow))).all()
    assert [r.ledger_id for r in rows] == [10578187002]
