"""GET /v2/trades/f{symbol}/hist — public funding executions, [ID, MTS, AMOUNT, RATE, PERIOD]."""
import httpx
import pytest

from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST, FundingTrade


def _client(handler) -> tuple[httpx.AsyncClient, BitfinexREST]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return http, BitfinexREST(
        http=http, base_url="https://api-pub.bitfinex.com", limiter=FundingRateLimiter())


async def test_get_funding_trades_parses_rows_and_sends_start_limit_and_ascending_sort():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["params"] = request.url.path, dict(request.url.params)
        return httpx.Response(200, json=[[1, 1_700_000_000_000, -250.5, 0.0002, 2],
                                         [2, 1_700_000_001_000, 100, 0.00021, 30]])

    http, rest = _client(handler)
    try:
        rows = await rest.get_funding_trades(symbol="fUST", start=1_699_999_999_999, limit=500)
    finally:
        await http.aclose()
    assert seen["path"] == "/v2/trades/fUST/hist"
    assert seen["params"] == {"start": "1699999999999", "limit": "500", "sort": "1"}
    assert rows[0] == FundingTrade(1, 1_700_000_000_000, -250.5, 0.0002, 2)
    assert rows[1].period == 30


async def test_get_funding_trades_tolerates_a_bare_currency():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/trades/fUSD/hist"
        return httpx.Response(200, json=[])

    http, rest = _client(handler)
    try:
        assert await rest.get_funding_trades(symbol="USD", start=0) == []
    finally:
        await http.aclose()


@pytest.mark.parametrize("body", [[[1, 2, 3]], {"error": 1}, [[1, "x", 1, 1, 1]]])
async def test_get_funding_trades_rejects_a_malformed_body(body):
    http, rest = _client(lambda request: httpx.Response(200, json=body))
    try:
        with pytest.raises(BitfinexShapeError):
            await rest.get_funding_trades(symbol="fUST", start=0)
    finally:
        await http.aclose()


async def test_get_funding_trades_raises_on_an_http_error():
    http, rest = _client(lambda request: httpx.Response(500, text="boom"))
    try:
        with pytest.raises(BitfinexAPIError):
            await rest.get_funding_trades(symbol="fUST", start=0)
    finally:
        await http.aclose()
