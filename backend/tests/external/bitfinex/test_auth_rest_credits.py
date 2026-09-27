"""ActiveFundingCredit parse + REST fetch unit tests.

Parallels test_auth_rest.py pattern for the credits endpoint.
Bitfinex /v2/auth/r/funding/credits/{symbol} and /loans/{symbol} share one
positional layout:
  [0]=ID [1]=SYMBOL [2]=SIDE [3]=MTS_CREATE [4]=MTS_UPDATE [5]=AMOUNT
  [6]=FLAGS [7]=STATUS [8]=RATE_TYPE [9]=_ [10]=_ [11]=RATE [12]=PERIOD ...
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingCredit,
    BitfinexAuthREST,
    parse_active_funding_credits,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

_CREDITS_PATH = "v2/auth/r/funding/credits"


def _credit_row(
    credit_id: int = 98765,
    symbol: str = "fUST",
    amount: float = 150.0,
    status: str = "ACTIVE",
    rate: float = 0.00031,
    period: int = 2,
) -> list:
    # 19-element funding credit array; only documented indices are meaningful.
    row: list = [None] * 19
    row[0] = credit_id
    row[1] = symbol
    row[2] = 1                    # SIDE: 1 = lender
    row[3] = 1_700_000_000_000    # MTS_CREATE
    row[4] = 1_700_000_001_000    # MTS_UPDATE
    row[5] = amount               # AMOUNT (positive for lender)
    row[7] = status               # STATUS
    row[11] = rate                # RATE
    row[12] = period              # PERIOD
    return row


# Verbatim rows returned by Bitfinex on 2026-09-23. [9] and [10] are null; a
# parser reading rate/period there silently produced 0 for both.
_LIVE_LOAN_ROW = [61621685, "fUST", 1, 1790100510000, 1790100510000, 150.77638588, 0,
                  "ACTIVE", "FIXED", None, None, 0.0001482, 2, 1790100510000,
                  1790100510000, None, 0, None, 0, None, 0]
_LIVE_CREDIT_ROW = [464746163, "fUST", 1, 1787933444000, 1788088327000, 392.30178005,
                    None, "CLOSED (expired)", "FIXED", None, None, 0.000178, 2,
                    1787933444000, 1788106423000, None, 0, None, 0, None, 0, "tBTCUST"]


def test_parse_reads_rate_and_period_where_the_venue_puts_them():
    loan, credit = parse_active_funding_credits([_LIVE_LOAN_ROW, _LIVE_CREDIT_ROW])
    assert (loan.rate, loan.period_days, loan.amount) == (0.0001482, 2, Decimal("150.77638588"))
    assert (credit.rate, credit.period_days) == (0.000178, 2)


def test_parse_keeps_mts_opening_apart_from_mts_create():
    """[13] is the originating trade's instant. Loan 61621685 was opened at
    09-22 18:08:30Z and became credit 466451710 with a later MTS_CREATE but the
    same opening (that credit's create time here is illustrative)."""
    converted = [466451710, *_LIVE_LOAN_ROW[1:3], 1790104110000, 1790104110000,
                 *_LIVE_LOAN_ROW[5:]]
    loan, credit = parse_active_funding_credits([_LIVE_LOAN_ROW, converted])
    assert loan.mts_opening == credit.mts_opening == 1790100510000
    assert credit.mts_created == 1790104110000
    assert parse_active_funding_credits([_LIVE_LOAN_ROW[:13]])[0].mts_opening is None


def test_a_row_too_short_to_hold_the_period_is_rejected():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_credits([_LIVE_LOAN_ROW[:12]])


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="KEY", api_secret="SECRET"),
        allocation_cap_usdt=Decimal("1000"),
    )


# ── parse_active_funding_credits ──────────────────────────────────────────────


def test_parse_happy_path():
    credits = parse_active_funding_credits([_credit_row()])
    assert credits == [
        ActiveFundingCredit(
            credit_id="98765",
            symbol="fUST",
            amount=Decimal("150.0"),
            rate=0.00031,
            period_days=2,
            status="ACTIVE",
            mts_created=1_700_000_000_000,
            mts_updated=1_700_000_001_000,
        )
    ]


def test_parse_empty():
    assert parse_active_funding_credits([]) == []


def test_parse_rejects_non_list():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_credits({"not": "a list"})


def test_parse_rejects_short_row():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_credits([[1, "fUST", 1, 0, 0, 100.0]])  # len < 11


def test_parse_amount_is_absolute():
    """Venue may return negative amount (borrower-side); we normalize to abs."""
    credits = parse_active_funding_credits([_credit_row(amount=-150.0)])
    assert credits[0].amount == Decimal("150.0")


def test_parse_multiple_credits_sum():
    """Three credits → three ActiveFundingCredit rows; Σamount = 450."""
    rows = [
        _credit_row(credit_id=1, amount=150.0),
        _credit_row(credit_id=2, amount=150.0),
        _credit_row(credit_id=3, amount=150.0),
    ]
    credits = parse_active_funding_credits(rows)
    assert len(credits) == 3
    total = sum(c.amount for c in credits)
    assert total == Decimal("450.0")


# ── BitfinexAuthREST.get_active_funding_credits ───────────────────────────────


@pytest.mark.asyncio
async def test_get_active_funding_credits_signs_and_parses():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=[_credit_row()])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 111))
        credits = await client.get_active_funding_credits(ctx=_ctx(), symbol="fUST")

    assert len(credits) == 1
    assert credits[0].credit_id == "98765"
    assert credits[0].amount == Decimal("150.0")
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/funding/credits/fUST"
    headers = captured["headers"]
    assert headers["bfx-apikey"] == "KEY"
    assert "bfx-signature" in headers


@pytest.mark.asyncio
async def test_get_active_funding_credits_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 1))
        with pytest.raises(BitfinexAPIError):
            await client.get_active_funding_credits(ctx=_ctx(), symbol="fUST")


@pytest.mark.asyncio
async def test_get_active_funding_credits_empty_response():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 1))
        credits = await client.get_active_funding_credits(ctx=_ctx(), symbol="fUST")
    assert credits == []


@pytest.mark.asyncio
async def test_get_active_funding_credits_without_symbol_fetches_all():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[_credit_row()])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 111))
        credits = await client.get_active_funding_credits(ctx=_ctx())

    assert len(credits) == 1
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/funding/credits"


@pytest.mark.asyncio
async def test_loans_are_read_from_their_own_endpoint_and_namespaced():
    """A filled offer is a loan until a borrower draws it into a position, and
    Bitfinex lists it only under /loans until then. The id is namespaced because
    loan and credit ids are separate venue sequences."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[_LIVE_LOAN_ROW])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 1))
        loans = await client.get_active_funding_loans(ctx=_ctx(), symbol="fUST")

    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/funding/loans/fUST"
    assert [loan.credit_id for loan in loans] == ["loan:61621685"]
    assert loans[0].amount == Decimal("150.77638588")
    assert loans[0].status == "ACTIVE"
