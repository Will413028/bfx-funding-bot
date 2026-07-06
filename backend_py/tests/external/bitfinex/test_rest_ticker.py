"""GET /v2/ticker/f{symbol} — E2 book-aware clamp 的唯一 book 資料源。

REST funding ticker 為 17 欄 flat array（WS 版 16 欄 + 尾端 FIRST_TRADE）：
[FRR, BID, BID_PERIOD, BID_SIZE, ASK, ASK_PERIOD, ASK_SIZE, DAILY_CHANGE,
 DAILY_CHANGE_PERC, LAST_PRICE, VOLUME, HIGH, LOW, _PLH, _PLH,
 FRR_AMOUNT_AVAILABLE, FIRST_TRADE]
clamp 只需前 7 欄。
"""
import httpx
import pytest

from bfx_funding_bot.external.bitfinex.errors import (
    BitfinexAPIError,
    BitfinexRateLimited,
    BitfinexShapeError,
)
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST, FundingTicker

_ROW = [
    0.0002, 0.00018, 2, 50_000.0, 0.00021, 30, 120_000.0,
    0.00001, 0.05, 0.0002, 1_000_000.0, 0.00025, 0.00015,
    None, None, 250_000.0, 0.00019,
]


def _client(handler) -> tuple[httpx.AsyncClient, BitfinexREST]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rest = BitfinexREST(
        http=http, base_url="https://api-pub.bitfinex.com",
        limiter=FundingRateLimiter(),
    )
    return http, rest


async def test_get_funding_ticker_parses_funding_array():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ROW)

    http, rest = _client(handler)
    async with http:
        t = await rest.get_funding_ticker(symbol="fUST")
    assert captured["url"] == "https://api-pub.bitfinex.com/v2/ticker/fUST"
    assert t == FundingTicker(
        symbol="fUST", frr=0.0002, bid=0.00018, bid_period=2, bid_size=50_000.0,
        ask=0.00021, ask_period=30, ask_size=120_000.0,
    )


async def test_get_funding_ticker_tolerates_missing_f_prefix():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ROW)

    http, rest = _client(handler)
    async with http:
        t = await rest.get_funding_ticker(symbol="UST")
    assert captured["url"].endswith("/v2/ticker/fUST")
    assert t.symbol == "fUST"


async def test_get_funding_ticker_raises_on_http_error():
    http, rest = _client(lambda r: httpx.Response(500, text="boom"))
    async with http:
        with pytest.raises(BitfinexAPIError):
            await rest.get_funding_ticker(symbol="fUST")


async def test_get_funding_ticker_raises_on_429():
    http, rest = _client(lambda r: httpx.Response(429, headers={"Retry-After": "5"}))
    async with http:
        with pytest.raises(BitfinexRateLimited):
            await rest.get_funding_ticker(symbol="fUST")


async def test_get_funding_ticker_rejects_non_list():
    http, rest = _client(lambda r: httpx.Response(200, json={"oops": 1}))
    async with http:
        with pytest.raises(BitfinexShapeError):
            await rest.get_funding_ticker(symbol="fUST")


async def test_get_funding_ticker_rejects_short_row():
    http, rest = _client(lambda r: httpx.Response(200, json=[0.0002, 0.00018]))
    async with http:
        with pytest.raises(BitfinexShapeError):
            await rest.get_funding_ticker(symbol="fUST")


async def test_get_funding_ticker_rejects_null_book_field():
    row = list(_ROW)
    row[1] = None  # BID null（空 book 邊）→ fail-closed，讓呼叫方 fallback
    http, rest = _client(lambda r: httpx.Response(200, json=row))
    async with http:
        with pytest.raises(BitfinexShapeError):
            await rest.get_funding_ticker(symbol="fUST")
