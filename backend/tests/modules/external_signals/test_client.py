"""ExternalSignalsClient tests: URL/param assembly, nested unwrap, 429 retry."""
from __future__ import annotations

import httpx
import pytest

from bfx_funding_bot.modules.external_signals.client import ExternalSignalsClient

_DERIV_ENTRY = [
    1784471455000, None, 64493.256542075, 64453, None, 65301276.4605679,
    None, 1784476800000, 0.00047447, 7522, None, 0.00009266, None, None,
    64455.8253, None, None, 8873.99876212, None, None, None, 0.0005, 0.0025,
]
_LIQ_ENTRY = [
    "pos", 193027628, 1784428435202, None, "tETHF0:USTF0",
    -0.1, 1845.5, None, 1, 1, None, 1877.2,
]
_BINANCE_ENTRY = {
    "symbol": "BTCUSDT",
    "fundingTime": 1784419200003,
    "fundingRate": "0.00000406",
    "markPrice": "",
}


def _fast_kwargs() -> dict[str, object]:
    from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter

    return {
        "deriv_limiter": FundingRateLimiter(1000, 1.0),
        "liq_limiter": FundingRateLimiter(1000, 1.0),
        "binance_limiter": FundingRateLimiter(1000, 1.0),
        "rate_limit_sleep": 0.0,
        "liq_rate_limit_sleep": 0.0,
        "transport_error_sleep": 0.0,
    }


def _client(handler: httpx.MockTransport) -> tuple[ExternalSignalsClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=handler)
    return ExternalSignalsClient(http=http, **_fast_kwargs()), http  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_deriv_status_hist_url_and_parse() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_DERIV_ENTRY])

    client, http = _client(httpx.MockTransport(handler))
    async with http:
        recs = await client.get_deriv_status_hist(
            symbol="tBTCF0:USTF0", end=1784471455000, limit=5000
        )
    assert seen[0].url.host == "api-pub.bitfinex.com"
    assert seen[0].url.path == "/v2/status/deriv/tBTCF0:USTF0/hist"
    assert seen[0].url.params["end"] == "1784471455000"
    assert seen[0].url.params["limit"] == "5000"
    assert len(recs) == 1
    assert recs[0].funding_rate == 0.00009266


@pytest.mark.asyncio
async def test_liquidations_unwraps_nested_entries_and_skips_bad() -> None:
    bad = list(_LIQ_ENTRY)
    bad[1] = None  # null pos_id → skipped, not fatal

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[_LIQ_ENTRY], [bad], [[*_LIQ_ENTRY[:11], None]]])

    client, http = _client(httpx.MockTransport(handler))
    async with http:
        recs = await client.get_liquidations_hist(end=1784471455000, limit=500)
    assert len(recs) == 2
    assert recs[0].pos_id == 193027628
    assert recs[1].price_acquired is None


@pytest.mark.asyncio
async def test_http_429_retries_then_succeeds() -> None:
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        if n["count"] == 1:
            return httpx.Response(429, json=["error", 11010, "ratelimit: error"])
        return httpx.Response(200, json=[_DERIV_ENTRY])

    client, http = _client(httpx.MockTransport(handler))
    async with http:
        recs = await client.get_deriv_status_hist(symbol="tBTCF0:USTF0", end=1, limit=10)
    assert n["count"] == 2
    assert len(recs) == 1


@pytest.mark.asyncio
async def test_soft_ratelimit_error_body_retries() -> None:
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        if n["count"] == 1:
            # Bitfinex sometimes answers HTTP 200 with an error array
            return httpx.Response(200, json=["error", 11010, "ratelimit: error"])
        return httpx.Response(200, json=[[_LIQ_ENTRY]])

    client, http = _client(httpx.MockTransport(handler))
    async with http:
        recs = await client.get_liquidations_hist(end=1, limit=10)
    assert n["count"] == 2
    assert len(recs) == 1


@pytest.mark.asyncio
async def test_retries_exhausted_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=["error", 11010, "ratelimit: error"])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ExternalSignalsClient(http=http, max_retries=2, **_fast_kwargs())  # type: ignore[arg-type]
    async with http:
        with pytest.raises(RuntimeError, match=r"rate.?limit"):
            await client.get_deriv_status_hist(symbol="tBTCF0:USTF0", end=1, limit=10)


@pytest.mark.asyncio
async def test_hard_http_error_raises_immediately() -> None:
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        return httpx.Response(500, text="boom")

    client, http = _client(httpx.MockTransport(handler))
    async with http:
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await client.get_deriv_status_hist(symbol="tBTCF0:USTF0", end=1, limit=10)
    assert n["count"] == 1


@pytest.mark.asyncio
async def test_binance_funding_url_and_parse() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_BINANCE_ENTRY])

    client, http = _client(httpx.MockTransport(handler))
    async with http:
        recs = await client.get_binance_funding(symbol="BTCUSDT", start_time=0, limit=1000)
    assert seen[0].url.host == "fapi.binance.com"
    assert seen[0].url.path == "/fapi/v1/fundingRate"
    assert seen[0].url.params["symbol"] == "BTCUSDT"
    assert seen[0].url.params["startTime"] == "0"
    assert recs[0].venue == "binance-usdm"
    assert recs[0].mark_price is None


@pytest.mark.asyncio
async def test_an_auth_failure_body_is_an_answer_and_is_not_retried() -> None:
    """10100 is ERR_AUTH_FAIL (seen in production as "apikey: digest invalid"),
    not a rate limit: retrying it for minutes would only hide the refusal."""
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        return httpx.Response(200, json=["error", 10100, "apikey: digest invalid"])

    client, http = _client(httpx.MockTransport(handler))
    async with http:
        with pytest.raises(RuntimeError, match="refused by the venue: 10100"):
            await client.get_liquidations_hist(end=1, limit=10)
    assert n["count"] == 1
