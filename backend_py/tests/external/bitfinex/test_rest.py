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
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat  # noqa: F401


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


@pytest.mark.asyncio
async def test_get_funding_stats_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=2&end=1715000000000",
        json=[
            [1715000000000, None, None, 5.8e-7, 2.3, None, None,
             4.5e7, 2.1e7, None, None, 1.2e6],
            [1714996400000, None, None, 5.7e-7, 2.4, None, None,
             4.4e7, 2.0e7, None, None, 1.1e6],
        ],
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        stats = await client.get_funding_stats(
            symbol="fUSD", end=1715000000000, limit=2,
        )

    assert len(stats) == 2
    assert stats[0].mts == 1715000000000
    assert stats[0].frr == Decimal("5.8e-7")
    assert stats[0].funding_amount == Decimal("45000000.0")
    assert all(s.symbol == "fUSD" for s in stats)


@pytest.mark.asyncio
async def test_get_funding_stats_strips_leading_f(httpx_mock: HTTPXMock) -> None:
    """Symbol 'fUSD' becomes 'fUSD' in URL path (the leading 'f' stays).

    Bitfinex's funding_stats path is /v2/funding/stats/{Symbol}/hist where
    Symbol is the full funding symbol (with leading f). Verify call shape.
    """
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=1&end=0",
        json=[[
            1715000000000, None, None, 5.8e-7, 2.3, None, None,
            4.5e7, 2.1e7, None, None, 1.2e6,
        ]],
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        stats = await client.get_funding_stats(symbol="fUSD", end=0, limit=1)
    assert len(stats) == 1


@pytest.mark.asyncio
async def test_get_funding_stats_429_raises_rate_limited(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=10&end=0",
        status_code=429,
        headers={"Retry-After": "8"},
        text='["error", 11010, "ratelimit: error"]',
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        with pytest.raises(BitfinexRateLimited) as exc_info:
            await client.get_funding_stats(symbol="fUSD", end=0, limit=10)
        assert exc_info.value.retry_after_seconds == 8.0


@pytest.mark.asyncio
async def test_get_funding_stats_500_raises_api_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=10&end=0",
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
            await client.get_funding_stats(symbol="fUSD", end=0, limit=10)
        assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_get_funding_stats_unexpected_shape_raises_shape_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/funding/stats/fUSD/hist?limit=10&end=0",
        json={"unexpected": "object instead of array"},
    )

    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        with pytest.raises(BitfinexShapeError):
            await client.get_funding_stats(symbol="fUSD", end=0, limit=10)


@pytest.mark.asyncio
async def test_get_funding_candles_a30_uses_extended_path(
    httpx_mock: HTTPXMock,
) -> None:
    """period_agg='a30' must resolve to URL 'a30:p2:p30' (Bitfinex aggregate
    candles need explicit period range; the short form returns empty)."""
    httpx_mock.add_response(
        url="https://api-pub.bitfinex.com/v2/candles/trade:1h:fUSD:a30:p2:p30/hist?limit=2&start=0&end=1715000000000",
        json=[
            [1715000000000, 0.00011, 0.00013, 0.00014, 0.00011, 3281.30],
            [1714996400000, 0.00010, 0.00014, 0.00015, 0.00007, 1610647.43],
        ],
    )
    async with httpx.AsyncClient() as http:
        client = BitfinexREST(
            http=http,
            base_url="https://api-pub.bitfinex.com",
            limiter=FundingRateLimiter(max_rate=1000, time_period=1.0),
        )
        candles = await client.get_funding_candles(
            symbol="fUSD",
            timeframe="1h",
            period_agg="a30",
            start=0,
            end=1715000000000,
            limit=2,
        )
    assert len(candles) == 2
    # period_agg in the returned object stays as user-facing 'a30',
    # not URL form 'a30:p2:p30'
    assert all(c.period_agg == "a30" for c in candles)
