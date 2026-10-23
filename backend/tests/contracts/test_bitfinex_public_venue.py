"""Today's Bitfinex public endpoints against the parsers in external/bitfinex.

The canned samples in test_bitfinex_contract.py pin the layout we expect; these
tests ask the venue whether it still answers that way. They need the network but no
credentials, so they never run in a PR's CI: venue-contract.yml runs them once a day
and sends a failure to Telegram.

Each check goes through the client the bot itself uses, then bounds every column it
reads. The bounds are what catches a reordered row: rate and period swapped still
convert to float and int, so a parser alone would accept them.
"""
import time
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.funding_rules import RULE, FundingRules, minimum_amount
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST

pytestmark = [pytest.mark.integration, pytest.mark.venue_contract]

SYMBOLS = ("fUST", "fUSD")
DAY_MS = 86_400_000
# A daily rate above 1% is 3650% APR; Bitfinex has never cleared that for USD or UST.
MAX_DAILY_RATE = Decimal("0.01")
PERIODS = range(2, 121)
# The field's default, not Settings(): the workflow has no DATABASE_URL or .env.
BASE_URL: str = Settings.model_fields["bitfinex_api_base_url"].default


def _now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture
async def rest() -> AsyncIterator[BitfinexREST]:
    async with httpx.AsyncClient() as http:
        # One wait on a 429, so a shared runner IP being throttled is not reported as drift.
        yield BitfinexREST(
            http=http, base_url=BASE_URL,
            limiter=FundingRateLimiter(), rate_limit_backoff=(60.0,),
        )


@pytest.mark.parametrize("symbol", SYMBOLS)
async def test_funding_candles_parse_and_are_current(rest: BitfinexREST, symbol: str) -> None:
    now = _now_ms()
    candles = await rest.get_funding_candles(
        symbol=symbol, timeframe="1h", period_agg="p2", start=now - 2 * DAY_MS, end=now,
    )
    assert candles, "no 1h p2 candles in the last two days"
    assert candles[0].mts > now - DAY_MS, "newest candle is over a day old"
    assert [c.mts for c in candles] == sorted((c.mts for c in candles), reverse=True)
    for c in candles:
        assert c.open is not None and c.close is not None and c.high is not None
        assert c.low is not None and c.volume is not None
        assert 0 < c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high
        assert c.high <= MAX_DAILY_RATE
        assert c.volume >= 0


@pytest.mark.parametrize("symbol", SYMBOLS)
async def test_funding_stats_parse_and_are_current(rest: BitfinexREST, symbol: str) -> None:
    now = _now_ms()
    stats = await rest.get_funding_stats(symbol=symbol, end=now, limit=10)
    assert stats, "no funding_stats rows"
    assert stats[0].mts > now - DAY_MS, "newest funding_stats row is over a day old"
    for s in stats:
        assert s.frr is not None and 0 <= s.frr <= MAX_DAILY_RATE
        assert s.avg_period is not None and 2 <= s.avg_period <= 120
        assert s.funding_amount is not None and s.funding_amount > 0
        assert s.funding_amount_used is not None
        assert 0 <= s.funding_amount_used <= s.funding_amount
        assert s.funding_below_threshold is not None and s.funding_below_threshold >= 0


@pytest.mark.parametrize("symbol", SYMBOLS)
async def test_funding_book_parses_with_both_sides(rest: BitfinexREST, symbol: str) -> None:
    levels = await rest.get_funding_book(symbol=symbol, length=25)
    asks = [lv for lv in levels if lv.amount > 0]
    bids = [lv for lv in levels if lv.amount < 0]
    assert asks and bids, "the book lost a side: AMOUNT's sign no longer says ask or bid"
    for lv in levels:
        assert 0 < lv.rate <= MAX_DAILY_RATE
        assert lv.rate in PERIODS
        assert lv.count >= 1


@pytest.mark.parametrize("symbol", SYMBOLS)
async def test_funding_trades_parse_and_are_current(rest: BitfinexREST, symbol: str) -> None:
    now = _now_ms()
    trades = await rest.get_funding_trades(symbol=symbol, start=now - DAY_MS, limit=50)
    assert trades, "no public funding trades in the last day"
    assert [t.mts for t in trades] == sorted(t.mts for t in trades), "sort=1 is no longer oldest first"
    for t in trades:
        assert t.trade_id > 0
        assert now - DAY_MS <= t.mts <= now + 60_000
        assert t.amount != 0
        assert 0 < t.rate <= MAX_DAILY_RATE
        assert t.period in PERIODS


async def test_ust_usd_fx_meets_the_funding_rule() -> None:
    async with httpx.AsyncClient() as http:
        evidence = await FundingRules(http=http, clock=_now_ms).observe("fUST")
    # observe() already refused any shape but [Decimal]; a depeg this wide is drift
    # in the endpoint, or news the operator must hear either way.
    assert Decimal("0.9") < evidence.usd_per_unit < Decimal("1.1")
    floor = minimum_amount(evidence, symbol="fUST", now_ms=_now_ms())
    assert floor == floor.quantize(RULE.amount_quantum)
