"""HTTP client for external-signal sources (research-only; NOT the live path).

Deliberately separate from external.bitfinex.rest.BitfinexREST: the live REST
client raises immediately on 429 (correct for the trading daemon), while a
multi-hour research backfill must sleep-and-retry instead. Keeping retry
semantics here avoids touching live-path code.

Empirical rate-limit behaviour (probed 2026-07-19):
- /v2/status/deriv/{key}/hist: several rapid requests fine; stay <= ~18/min.
- /v2/liquidations/hist: EXTREMELY strict — a 2nd immediate request already
  429s, and the cool-down lasts minutes. Keep >= ~25s between requests and
  sleep >= ~4 min after any 429.
- Binance /fapi/v1/fundingRate: generous (weight-based); 1 req/s is safe.

429 appears both as HTTP 429 and as HTTP 200 with body
["error", 11010, "ratelimit: error"] — both are handled as retryable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.modules.external_signals.schemas import (
    LiquidationRecord,
    PerpFundingRecord,
)

logger = logging.getLogger(__name__)

_BITFINEX_BASE = "https://api-pub.bitfinex.com"
_BINANCE_BASE = "https://fapi.binance.com"


def _is_soft_ratelimit(payload: Any) -> bool:
    return (
        isinstance(payload, list)
        and len(payload) >= 2
        and payload[0] == "error"
        and payload[1] in (11010, 10100)
    )


class ExternalSignalsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        bitfinex_base_url: str = _BITFINEX_BASE,
        binance_base_url: str = _BINANCE_BASE,
        deriv_limiter: FundingRateLimiter | None = None,
        liq_limiter: FundingRateLimiter | None = None,
        binance_limiter: FundingRateLimiter | None = None,
        max_retries: int = 8,
        rate_limit_sleep: float = 75.0,
        liq_rate_limit_sleep: float = 240.0,
        transport_error_sleep: float = 10.0,
    ) -> None:
        self._http = http
        self._bfx_base = bitfinex_base_url.rstrip("/")
        self._binance_base = binance_base_url.rstrip("/")
        # deriv-status hist tolerates moderate rates; keep well under the ceiling.
        self._deriv_limiter = deriv_limiter or FundingRateLimiter(18, 60.0)
        # liquidations hist: ~1 request per 25s steady state.
        self._liq_limiter = liq_limiter or FundingRateLimiter(1, 25.0)
        self._binance_limiter = binance_limiter or FundingRateLimiter(60, 60.0)
        self._max_retries = max_retries
        self._rate_limit_sleep = rate_limit_sleep
        self._liq_rate_limit_sleep = liq_rate_limit_sleep
        self._transport_error_sleep = transport_error_sleep

    async def _get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        limiter: FundingRateLimiter,
        rate_limit_sleep: float,
    ) -> Any:
        last_reason = "unknown"
        for attempt in range(self._max_retries + 1):
            try:
                async with limiter.acquire():
                    resp = await self._http.get(url, params=params, timeout=30.0)
            except httpx.HTTPError as e:
                last_reason = f"transport error: {e!r}"
                logger.warning(
                    "GET %s transport error (attempt %d/%d): %r",
                    url, attempt + 1, self._max_retries + 1, e,
                )
                await asyncio.sleep(self._transport_error_sleep)
                continue

            if resp.status_code == 429:
                last_reason = "HTTP 429"
                logger.warning(
                    "GET %s rate-limited (HTTP 429), sleeping %.0fs (attempt %d/%d)",
                    url, rate_limit_sleep, attempt + 1, self._max_retries + 1,
                )
                await asyncio.sleep(rate_limit_sleep)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"GET {url} failed: HTTP {resp.status_code}: {resp.text[:200]}"
                )

            payload = resp.json()
            if _is_soft_ratelimit(payload):
                last_reason = f"soft ratelimit body: {payload!r}"
                logger.warning(
                    "GET %s soft-rate-limited (%r), sleeping %.0fs (attempt %d/%d)",
                    url, payload, rate_limit_sleep, attempt + 1, self._max_retries + 1,
                )
                await asyncio.sleep(rate_limit_sleep)
                continue
            return payload

        raise RuntimeError(
            f"GET {url} gave up after {self._max_retries + 1} attempts "
            f"(rate-limited or transport errors; last: {last_reason})"
        )

    async def get_deriv_status_hist(
        self, *, symbol: str, end: int, limit: int = 5000
    ) -> list[PerpFundingRecord]:
        url = f"{self._bfx_base}/v2/status/deriv/{symbol}/hist"
        payload = await self._get_json(
            url,
            {"end": end, "limit": limit},
            limiter=self._deriv_limiter,
            rate_limit_sleep=self._rate_limit_sleep,
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"unexpected deriv status payload: {payload!r}")
        return [
            PerpFundingRecord.from_bitfinex(entry, symbol=symbol) for entry in payload
        ]

    async def get_liquidations_hist(
        self, *, end: int, limit: int = 500
    ) -> list[LiquidationRecord]:
        url = f"{self._bfx_base}/v2/liquidations/hist"
        payload = await self._get_json(
            url,
            {"end": end, "limit": limit},
            limiter=self._liq_limiter,
            rate_limit_sleep=self._liq_rate_limit_sleep,
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"unexpected liquidations payload: {payload!r}")
        records: list[LiquidationRecord] = []
        skipped = 0
        # Each page item wraps one entry: [["pos", ...]] — unwrap one level.
        for wrapper in payload:
            entries = wrapper if wrapper and isinstance(wrapper[0], list) else [wrapper]
            for entry in entries:
                try:
                    records.append(LiquidationRecord.from_bitfinex(entry))
                except (ValueError, TypeError) as e:
                    skipped += 1
                    logger.warning("skipping unparseable liquidation entry: %s", e)
        if skipped:
            logger.warning("liquidations page: skipped %d unparseable entries", skipped)
        return records

    async def get_binance_funding(
        self, *, symbol: str, start_time: int, limit: int = 1000
    ) -> list[PerpFundingRecord]:
        url = f"{self._binance_base}/fapi/v1/fundingRate"
        payload = await self._get_json(
            url,
            {"symbol": symbol, "startTime": start_time, "limit": limit},
            limiter=self._binance_limiter,
            rate_limit_sleep=self._rate_limit_sleep,
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"unexpected binance funding payload: {payload!r}")
        return [PerpFundingRecord.from_binance(entry) for entry in payload]
