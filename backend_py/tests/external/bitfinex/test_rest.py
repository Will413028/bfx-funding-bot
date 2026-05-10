from decimal import Decimal

import httpx
import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.external.bitfinex.errors import (
    BitfinexAPIError,
    BitfinexRateLimited,
    BitfinexShapeError,
)
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST


@pytest.mark.asyncio
async def test_get_funding_candles_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/candles/trade:1h:fUST:p2/hist?limit=125&start=1704067200000&end=1704153600000",
        json=[
            [1704153600000, 0.000125, 0.000128, 0.000130, 0.000122, 5000.5],
            [1704067200000, 0.000120, 0.000125, 0.000128, 0.000118, 4500.2],
        ],
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        candles = await client.get_funding_candles(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            start=1704067200000,
            end=1704153600000,
            limit=125,
        )

    assert len(candles) == 2
    assert candles[0].mts == 1704153600000
    assert candles[0].open == Decimal("0.000125")
    assert candles[1].volume == Decimal("4500.2")
    assert all(c.symbol == "fUST" for c in candles)
    assert all(c.period_agg == "p2" for c in candles)


@pytest.mark.asyncio
async def test_get_funding_candles_429_raises_rate_limited(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/candles/trade:1h:fUST:p2/hist?limit=10&start=0&end=0",
        status_code=429,
        headers={"Retry-After": "12"},
        text='["error", 11010, "ratelimit: error"]',
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        with pytest.raises(BitfinexRateLimited) as exc_info:
            await client.get_funding_candles(
                symbol="fUST",
                timeframe="1h",
                period_agg="p2",
                start=0,
                end=0,
                limit=10,
            )
        assert exc_info.value.retry_after_seconds == 12.0


@pytest.mark.asyncio
async def test_get_funding_candles_500_raises_api_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/candles/trade:1h:fUST:p2/hist?limit=10&start=0&end=0",
        status_code=500,
        text="internal error",
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        with pytest.raises(BitfinexAPIError) as exc_info:
            await client.get_funding_candles(
                symbol="fUST",
                timeframe="1h",
                period_agg="p2",
                start=0,
                end=0,
                limit=10,
            )
        assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_get_funding_candles_unexpected_shape_raises_shape_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/candles/trade:1h:fUST:p2/hist?limit=10&start=0&end=0",
        json={"unexpected": "object instead of array"},
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        with pytest.raises(BitfinexShapeError):
            await client.get_funding_candles(
                symbol="fUST",
                timeframe="1h",
                period_agg="p2",
                start=0,
                end=0,
                limit=10,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_funding_candles_against_real_bitfinex() -> None:
    """Pulls last-24h fUST 1h candles from real Bitfinex.

    Skipped by default; run with: `uv run pytest -m integration`.
    """
    import time

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(),
        )
        end = int(time.time() * 1000)
        start = end - 24 * 60 * 60 * 1000
        candles = await client.get_funding_candles(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            start=start,
            end=end,
            limit=24,
        )
        assert len(candles) > 0, "Bitfinex returned no candles for last 24h fUST 1h"
        assert all(c.symbol == "fUST" for c in candles)
        assert all(c.timeframe == "1h" for c in candles)
        assert all(c.mts > 0 for c in candles)
