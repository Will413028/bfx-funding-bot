"""ActiveFundingCredit parse + REST fetch unit tests.

Parallels test_auth_rest.py pattern for the credits endpoint.
Bitfinex /v2/auth/r/funding/credits/{symbol} positional array layout:
  [0]=ID [1]=SYMBOL [2]=SIDE [3]=MTS_CREATE [4]=MTS_UPDATE [5]=AMOUNT
  [6]=FLAGS [7]=STATUS [8]=RATE_TYPE [9]=RATE [10]=PERIOD ...
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
    row[9] = rate                 # RATE
    row[10] = period              # PERIOD
    return row


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
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 111)
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
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        with pytest.raises(BitfinexAPIError):
            await client.get_active_funding_credits(ctx=_ctx(), symbol="fUST")


@pytest.mark.asyncio
async def test_get_active_funding_credits_empty_response():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        credits = await client.get_active_funding_credits(ctx=_ctx(), symbol="fUST")
    assert credits == []
