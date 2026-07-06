import json
import logging
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


@dataclass(frozen=True, slots=True)
class FundingTicker:
    """GET /v2/ticker/{fSymbol} — funding book 快照（E2 book-aware clamp）。

    Rate 欄位是日利率 decimal（0.0002 = 0.02%/day），與 funding_candles.close
    同尺度，可直接與 StandingQuote.rate 比較。funding_stats 的 frr（~1e-6）
    是不同量，勿混用（see live_attribution.assert_market_rate_band）。
    """
    symbol: str
    frr: float          # Flash Return Rate（過去 1h 平均固定利率）
    bid: float          # best bid rate（借方最高出價；我方吃單即成交）
    bid_period: int     # bid 天期 — taker fill 會繼承這個 period
    bid_size: float
    ask: float          # best ask rate（貸方最低要價 = 隊首）
    ask_period: int
    ask_size: float

    @classmethod
    def from_bitfinex(cls, entry: list[Any], *, symbol: str) -> "FundingTicker":
        # REST 回 17 欄（WS 16 欄 + FIRST_TRADE）；clamp 只需前 7 欄。
        if len(entry) < 7:
            raise ValueError(f"funding ticker too short: {len(entry)} fields")
        head = entry[:7]
        if any(v is None for v in head):
            # 空 book 邊 → fail-closed：讓呼叫方走 fallback（= 無 clamp）
            raise ValueError(f"null field in funding ticker head: {head!r}")
        return cls(
            symbol=symbol,
            frr=float(entry[0]),
            bid=float(entry[1]),
            bid_period=int(entry[2]),
            bid_size=float(entry[3]),
            ask=float(entry[4]),
            ask_period=int(entry[5]),
            ask_size=float(entry[6]),
        )


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
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._limiter = limiter

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

        async with self._limiter.acquire():
            try:
                resp = await self._http.get(
                    f"{self._base_url}{path}", params=params, timeout=30.0
                )
            except httpx.HTTPError as e:
                raise BitfinexAPIError(
                    status_code=0, message=f"transport error: {e}", raw=None
                ) from e

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                retry_after_seconds = float(retry_after) if retry_after else None
            except ValueError:
                retry_after_seconds = None
            raise BitfinexRateLimited(retry_after_seconds=retry_after_seconds)

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

        async with self._limiter.acquire():
            try:
                resp = await self._http.get(
                    f"{self._base_url}{path}", params=params, timeout=30.0
                )
            except httpx.HTTPError as e:
                raise BitfinexAPIError(
                    status_code=0, message=f"transport error: {e}", raw=None
                ) from e

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                retry_after_seconds = float(retry_after) if retry_after else None
            except ValueError:
                retry_after_seconds = None
            raise BitfinexRateLimited(retry_after_seconds=retry_after_seconds)

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

    async def get_funding_ticker(self, *, symbol: str) -> FundingTicker:
        """Pull the live funding ticker (best bid/ask) for one symbol.

        Endpoint: GET /v2/ticker/f{symbol}（免認證；funding schema 17 欄）

        E2 book-aware clamp 的唯一 book 資料源（docs/research/
        2026-07-06-profit-design-review.md §1 E2）。每 ~90s reconcile tick
        每 symbol 一 call，走共用 FundingRateLimiter（30/min budget）。

        Args:
            symbol: e.g. "fUST", "fUSD". Tolerates leading "f".
        """
        sym = symbol[1:] if symbol.startswith("f") else symbol
        path = f"/v2/ticker/f{sym}"

        async with self._limiter.acquire():
            try:
                resp = await self._http.get(f"{self._base_url}{path}", timeout=30.0)
            except httpx.HTTPError as e:
                raise BitfinexAPIError(
                    status_code=0, message=f"transport error: {e}", raw=None
                ) from e

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                retry_after_seconds = float(retry_after) if retry_after else None
            except ValueError:
                retry_after_seconds = None
            raise BitfinexRateLimited(retry_after_seconds=retry_after_seconds)

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
                f"expected funding ticker list, got {type(payload).__name__}: {payload!r}"
            )

        try:
            return FundingTicker.from_bitfinex(payload, symbol=f"f{sym}")
        except (ValueError, TypeError) as e:
            raise BitfinexShapeError(
                f"funding ticker parse failed: {e}; raw={payload!r}"
            ) from e
