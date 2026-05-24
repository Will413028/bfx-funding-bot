from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    BitfinexAuthREST,
    parse_active_funding_offers,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials


def _row(offer_id=12345, symbol="fUSD", mts=1_700_000_000_000, amount=-100.0,
         status="ACTIVE", rate=0.00031, period=2):
    # 21-element funding offer row; only documented indices are meaningful.
    row = [None] * 21
    row[0] = offer_id
    row[1] = symbol
    row[2] = mts
    row[4] = amount        # negative for an offer; we store abs
    row[5] = amount  # index 5 = AMOUNT_ORIG, not parsed
    row[10] = status
    row[14] = rate
    row[15] = period
    return row


def test_parse_happy_path():
    offers = parse_active_funding_offers([_row()])
    assert offers == [
        ActiveFundingOffer(
            venue_offer_id="12345", symbol="fUSD", amount=Decimal("100.0"),
            rate=0.00031, period_days=2, mts_created=1_700_000_000_000,
            status="ACTIVE",
        )
    ]


def test_parse_empty():
    assert parse_active_funding_offers([]) == []


def test_parse_rejects_non_list():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_offers({"not": "a list"})


def test_parse_rejects_short_row():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_offers([[1, "fUSD", 0]])  # len < 16


def _ctx():
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="KEY", api_secret="SECRET"),
        allocation_cap_usdt=Decimal("1000"),
    )


@pytest.mark.asyncio
async def test_get_active_funding_offers_signs_and_parses():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content
        return httpx.Response(200, json=[_row()])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 111)
        offers = await client.get_active_funding_offers(ctx=_ctx(), symbol="fUSD")

    assert offers[0].venue_offer_id == "12345"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/funding/offers/fUSD"
    headers = captured["headers"]
    assert headers["bfx-apikey"] == "KEY"
    assert headers["bfx-nonce"] == "111"
    assert "bfx-signature" in headers and len(headers["bfx-signature"]) == 96  # sha384 hexdigest
    assert captured["content"] == b"{}"


@pytest.mark.asyncio
async def test_get_active_funding_offers_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        with pytest.raises(BitfinexAPIError):
            await client.get_active_funding_offers(ctx=_ctx())
