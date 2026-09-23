"""GET /v2/book/f{symbol}/P0 — funding book depth snapshot (ladder research
recording; E2 clamp keeps using the ticker).

Funding book rows are flat arrays [RATE, PERIOD, COUNT, AMOUNT];
AMOUNT > 0 = ask (funding offered), AMOUNT < 0 = bid (funding demand).
"""
import httpx

from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST, FundingBookLevel

_ROWS = [
    [0.00021, 30, 3, 120_000.0],    # ask
    [0.00022, 2, 1, 50_000.0],      # ask
    [0.00018, 2, 2, -80_000.0],     # bid
    [0.00017, 30, 1, -20_000.0],    # bid
]


def _client(handler) -> tuple[httpx.AsyncClient, BitfinexREST]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rest = BitfinexREST(
        http=http, base_url="https://api-pub.bitfinex.com",
        limiter=FundingRateLimiter(),
    )
    return http, rest


async def test_get_funding_book_parses_levels_and_url():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ROWS)

    http, rest = _client(handler)
    try:
        levels = await rest.get_funding_book(symbol="fUST", length=25)
    finally:
        await http.aclose()

    assert "/v2/book/fUST/P0" in captured["url"]
    assert "len=25" in captured["url"]
    assert len(levels) == 4
    first = levels[0]
    assert isinstance(first, FundingBookLevel)
    assert first.rate == 0.00021
    assert first.period == 30
    assert first.count == 3
    assert first.amount == 120_000.0


async def test_get_funding_book_tolerates_leading_f():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v2/book/fUSD/P0" in str(request.url)
        return httpx.Response(200, json=[])

    http, rest = _client(handler)
    try:
        levels = await rest.get_funding_book(symbol="USD")
    finally:
        await http.aclose()
    assert levels == []


async def test_get_funding_book_rejects_malformed_row():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[0.0002, 2]])  # too short

    http, rest = _client(handler)
    try:
        import pytest

        from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
        with pytest.raises(BitfinexShapeError):
            await rest.get_funding_book(symbol="fUST")
    finally:
        await http.aclose()
