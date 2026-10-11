import json
from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    BitfinexAuthREST,
    parse_active_funding_offers,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials


def _row(offer_id=12345, symbol="fUSD", mts=1_700_000_000_000, amount=-100.0,
         status="ACTIVE", rate=0.00031, period=2):
    # 21-element funding offer row; only documented indices are meaningful.
    row = [None] * 21
    row[0] = offer_id
    row[1] = symbol
    row[2] = mts
    row[3] = mts  # mts_update; required int (was None — exposed by shared parser)
    row[4] = amount        # negative for an offer; we store abs
    row[5] = amount  # index 5 = AMOUNT_ORIG, not parsed
    row[10] = status
    row[14] = rate
    row[15] = period
    return row


def _wire_body_with_rate(rate_literal: str, *, status: str = "ACTIVE") -> bytes:
    encoded = json.dumps([_row(status=status, rate="__EXACT_RATE__")])
    return encoded.replace('"__EXACT_RATE__"', rate_literal).encode("ascii")


def test_parse_happy_path():
    offers = parse_active_funding_offers([_row()])
    assert offers == [
        ActiveFundingOffer(
            venue_offer_id="12345", symbol="fUSD", amount=Decimal("100.0"),
            rate=0.00031, period_days=2, mts_created=1_700_000_000_000,
            status="ACTIVE",
            amount_original=Decimal("100.0"),
            mts_updated=1_700_000_000_000,
            rate_decimal=Decimal("0.00031"),
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
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 111))
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
async def test_typed_active_offer_preserves_wire_decimal_while_raw_stays_float():
    precise_rate = "0.0003100000000000000001"
    body = _wire_body_with_rate(precise_rate)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(iter((1, 2)).__next__))

        parsed = await client.get_active_funding_offers(ctx=_ctx(), symbol="fUSD")
        raw = await client.fetch_funding_offers_raw(ctx=_ctx(), symbol="fUSD")

    assert parsed[0].rate_decimal == Decimal(precise_rate)
    assert isinstance(raw[0][14], float)


@pytest.mark.asyncio
async def test_typed_offer_history_preserves_exact_wire_rate():
    precise_rate = "0.0003100000000000000001"
    body = _wire_body_with_rate(precise_rate, status="CANCELED")
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await BitfinexAuthREST(
            http=http,
            auth_gate=AuthRequestGate(iter((1,)).__next__),
        ).get_funding_offer_history(
            ctx=_ctx(),
            start_ms=1_699_999_999_000,
            end_ms=1_700_000_001_000,
        )

    assert result.offers[0].rate_decimal == Decimal(precise_rate)


@pytest.mark.asyncio
async def test_get_active_funding_offers_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 1))
        with pytest.raises(BitfinexAPIError):
            await client.get_active_funding_offers(ctx=_ctx(), symbol="fUSD")


@pytest.mark.asyncio
async def test_get_active_funding_offers_without_symbol_fetches_all_currencies():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 1))
        assert await client.get_active_funding_offers(ctx=_ctx()) == []
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/funding/offers"


@pytest.mark.asyncio
async def test_fetch_funding_offers_raw_returns_unparsed_body():
    rows = [_row(offer_id=1), _row(offer_id=2)]
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=rows))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 1))
        raw = await client.fetch_funding_offers_raw(ctx=_ctx(), symbol="fUSD")
    # raw is the positional-array body, NOT parsed ActiveFundingOffer objects
    assert raw == rows
    assert isinstance(raw, list) and isinstance(raw[0], list)
