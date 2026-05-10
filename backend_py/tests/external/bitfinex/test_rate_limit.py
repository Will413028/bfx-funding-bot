import asyncio

import pytest

from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter


@pytest.mark.asyncio
async def test_limiter_allows_burst_within_capacity() -> None:
    limiter = FundingRateLimiter(max_rate=5, time_period=1.0)
    start = asyncio.get_event_loop().time()
    for _ in range(5):
        async with limiter.acquire():
            pass
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_limiter_throttles_beyond_capacity() -> None:
    # max_rate=2 / time_period=0.5s → leaky bucket refills 1 token / 0.25s.
    # 4 calls: first 2 burst immediately, #3 waits 0.25s, #4 waits 0.5s total.
    limiter = FundingRateLimiter(max_rate=2, time_period=0.5)
    start = asyncio.get_event_loop().time()
    for _ in range(4):
        async with limiter.acquire():
            pass
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.5
