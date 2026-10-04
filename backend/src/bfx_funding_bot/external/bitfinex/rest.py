import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from bfx_funding_bot.external.bitfinex.errors import (
    BitfinexAPIError,
    BitfinexRateLimited,
    BitfinexShapeError,
)
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

logger = logging.getLogger(__name__)

# For batch jobs only (backfill, stats ingest). Bitfinex blocks an IP for about a
# minute after a 429, and research jobs share that IP with the live bot, so wait
# it out rather than fail the run. Each wait is the larger of Retry-After and
# the scheduled delay. The live daemon keeps the default (no retry): a blocked
# book fetch fails closed and the next cycle tries again.
BATCH_RATE_LIMIT_BACKOFF: tuple[float, ...] = (60.0, 120.0, 240.0, 240.0)


@dataclass(frozen=True, slots=True)
class FundingBookLevel:
    """One row of GET /v2/book/f{sym}/P0 — [RATE, PERIOD, COUNT, AMOUNT].

    AMOUNT > 0 = ask（貸方掛單）、< 0 = bid（借方需求）。rate 與 ticker/candle
    close 同尺度（日利率 decimal）。"""
    rate: float
    period: int
    count: int
    amount: float

    @classmethod
    def from_bitfinex(cls, entry: list[Any]) -> "FundingBookLevel":
        if not isinstance(entry, list) or len(entry) < 4:
            raise BitfinexShapeError(f"funding book row too short: {entry!r}")
        return cls(
            rate=float(entry[0]), period=int(entry[1]),
            count=int(entry[2]), amount=float(entry[3]),
        )


@dataclass(frozen=True, slots=True)
class FundingTrade:
    """One row of GET /v2/trades/f{sym}/hist -- [ID, MTS, AMOUNT, RATE, PERIOD].

    Public executions: AMOUNT's sign says which side was the taker, the volume is its
    magnitude. RATE is a daily decimal rate, as in the book."""
    trade_id: int
    mts: int
    amount: float
    rate: float
    period: int

    @classmethod
    def from_bitfinex(cls, entry: list[Any]) -> "FundingTrade":
        if not isinstance(entry, list) or len(entry) < 5:
            raise BitfinexShapeError(f"funding trade row too short: {entry!r}")
        try:
            return cls(
                trade_id=int(entry[0]), mts=int(entry[1]), amount=float(entry[2]),
                rate=float(entry[3]), period=int(entry[4]),
            )
        except (TypeError, ValueError) as e:
            raise BitfinexShapeError(f"funding trade parse failed: {e}; raw={entry!r}") from e


def _bitfinex_period_agg_path(period_agg: str) -> str:
    """Translate user-facing period_agg → Bitfinex URL period_agg.

    For aggregate candles (e.g. "a30"), Bitfinex requires an explicit
    period range like "a30:p2:p30". Empirically, "a30" alone returns [].
    Single-period values ("p2", "p30") pass through unchanged.
    """
    if period_agg.startswith("a") and ":" not in period_agg:
        return f"{period_agg}:p2:p30"
    return period_agg


class BitfinexREST:
    """Hand-rolled async REST client for Bitfinex public funding endpoints.

    Day-3 scope: ONE endpoint (get_funding_candles). Add others on demand.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        limiter: FundingRateLimiter,
        rate_limit_backoff: tuple[float, ...] = (),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._limiter = limiter
        self._rate_limit_backoff = rate_limit_backoff
        self._sleep = sleep

    async def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        """GET one public endpoint, waiting out 429s per `rate_limit_backoff`.

        Only idempotent public reads come through here. With the default empty
        backoff a 429 raises immediately, as it always has.
        """
        delays = iter(self._rate_limit_backoff)
        while True:
            async with self._limiter.acquire():
                try:
                    resp = await self._http.get(
                        f"{self._base_url}{path}", params=params, timeout=30.0
                    )
                except httpx.HTTPError as e:
                    raise BitfinexAPIError(
                        status_code=0, message=f"transport error: {e}", raw=None
                    ) from e
            if resp.status_code != 429:
                return resp
            retry_after = resp.headers.get("Retry-After")
            try:
                retry_after_seconds = float(retry_after) if retry_after else None
            except ValueError:
                retry_after_seconds = None
            delay = next(delays, None)
            if delay is None:
                raise BitfinexRateLimited(retry_after_seconds=retry_after_seconds)
            wait = max(delay, retry_after_seconds or 0.0)
            logger.warning("bitfinex rate limited on %s; retrying in %.0fs", path, wait)
            await self._sleep(wait)

    async def get_funding_candles(
        self,
        *,
        symbol: str,
        timeframe: str,
        period_agg: str,
        start: int,
        end: int,
        limit: int = 125,
    ) -> list[FundingCandle]:
        """Pull funding-rate candles.

        Endpoint: GET /v2/candles/trade:{tf}:f{symbol}:{period_agg}/hist
                  ?limit=N&start=MS&end=MS

        Returns candles in **descending** mts order (newest first), per Bitfinex.
        Pagination: max 10000 per call; default 125. start/end in **ms**.

        Args:
            symbol: e.g. "fUST", "fUSD". Tolerates leading "f".
            timeframe: "1m", "5m", "1h", "1D", etc.
            period_agg: aggregation key like "p2" (2-day), "a30" (avg of 30-day).
            start: ms timestamp inclusive lower bound.
            end: ms timestamp inclusive upper bound.
            limit: max candles, max 10000.
        """
        sym = symbol[1:] if symbol.startswith("f") else symbol
        url_period_agg = _bitfinex_period_agg_path(period_agg)
        path = f"/v2/candles/trade:{timeframe}:f{sym}:{url_period_agg}/hist"
        params = {"limit": limit, "start": start, "end": end}

        resp = await self._get(path, params)

        if resp.status_code >= 400:
            raise BitfinexAPIError(
                status_code=resp.status_code,
                message=resp.reason_phrase or "http error",
                raw=resp.text,
            )

        try:
            payload = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON: {e}") from e

        if not isinstance(payload, list):
            raise BitfinexShapeError(
                f"expected list of candles, got {type(payload).__name__}: {payload!r}"
            )

        candles: list[FundingCandle] = []
        for entry in payload:
            if not isinstance(entry, list):
                raise BitfinexShapeError(
                    f"expected each candle to be a list, got {type(entry).__name__}"
                )
            try:
                candles.append(
                    FundingCandle.from_bitfinex(
                        entry,
                        symbol=f"f{sym}",
                        timeframe=timeframe,
                        period_agg=period_agg,
                    )
                )
            except (ValueError, TypeError) as e:
                raise BitfinexShapeError(f"candle parse failed: {e}; raw={entry!r}") from e

        return candles

    async def get_funding_stats(
        self,
        *,
        symbol: str,
        end: int,
        limit: int = 250,
    ) -> list[FundingStat]:
        """Pull funding_stats rows.

        Endpoint: GET /v2/funding/stats/{symbol}/hist?limit=N&end=MS

        Returns rows in **descending** mts order (newest first), per Bitfinex.
        `end` in **ms**, inclusive upper bound.

        Bitfinex empirically caps this endpoint at limit=250 (500+ returns
        HTTP 500). The doc claims max 10000 but that is the funding_candles
        limit — funding_stats is stricter. Use the default unless you have
        verified larger pages work.

        Args:
            symbol: e.g. "fUSD", "fUST". Tolerates leading "f" already in
                place (path uses symbol as-is).
            end: ms timestamp inclusive upper bound.
            limit: max rows. Empirically capped at 250 by Bitfinex.
        """
        sym = symbol if symbol.startswith("f") else f"f{symbol}"
        path = f"/v2/funding/stats/{sym}/hist"
        params = {"limit": limit, "end": end}

        resp = await self._get(path, params)

        if resp.status_code >= 400:
            raise BitfinexAPIError(
                status_code=resp.status_code,
                message=resp.reason_phrase or "http error",
                raw=resp.text,
            )

        try:
            payload = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON: {e}") from e

        if not isinstance(payload, list):
            raise BitfinexShapeError(
                f"expected list of funding_stats rows, "
                f"got {type(payload).__name__}: {payload!r}"
            )

        stats: list[FundingStat] = []
        for entry in payload:
            if not isinstance(entry, list):
                raise BitfinexShapeError(
                    f"expected each row to be a list, got {type(entry).__name__}"
                )
            try:
                stats.append(FundingStat.from_bitfinex(entry, symbol=sym))
            except (ValueError, TypeError) as e:
                raise BitfinexShapeError(
                    f"funding_stats parse failed: {e}; raw={entry!r}"
                ) from e

        return stats

    async def get_funding_book(
        self, *, symbol: str, length: int = 25
    ) -> list[FundingBookLevel]:
        """Pull the live funding book depth (P0) for one symbol.

        Endpoint: GET /v2/book/f{sym}/P0?len={length}（免認證）。
        用途：定期 snapshot 落 `funding_book_snapshots`（自錄歷史 book——
        Bitfinex 不提供歷史 book，book-aware 策略的可回測資料只能從現在
        開始累積）。Live eligibility uses the separate reconciled in-memory
        funding-book provider; this REST response is its reconciliation input.
        """
        sym = symbol[1:] if symbol.startswith("f") else symbol
        path = f"/v2/book/f{sym}/P0"

        resp = await self._get(path, {"len": length})
        if resp.status_code != 200:
            raise BitfinexAPIError(
                status_code=resp.status_code, message=resp.text, raw=None
            )
        body = resp.json()
        if not isinstance(body, list):
            raise BitfinexShapeError(f"funding book: expected list, got {type(body).__name__}")
        return [FundingBookLevel.from_bitfinex(row) for row in body]

    async def get_funding_trades(
        self, *, symbol: str, start: int, limit: int = 1000
    ) -> list[FundingTrade]:
        """Public funding trades with ``mts >= start``, oldest first.

        Endpoint: GET /v2/trades/f{sym}/hist?start=MS&limit=N&sort=1 (no auth). Used to
        backfill and poll the simulated venue's fill volume; ``limit`` is the page size,
        the caller pages on the last ``mts`` returned.
        """
        sym = symbol[1:] if symbol.startswith("f") else symbol
        path = f"/v2/trades/f{sym}/hist"

        resp = await self._get(path, {"start": start, "limit": limit, "sort": 1})
        if resp.status_code != 200:
            raise BitfinexAPIError(
                status_code=resp.status_code, message=resp.text, raw=None
            )
        try:
            body = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON: {e}") from e
        if not isinstance(body, list):
            raise BitfinexShapeError(f"funding trades: expected list, got {type(body).__name__}")
        return [FundingTrade.from_bitfinex(row) for row in body]
