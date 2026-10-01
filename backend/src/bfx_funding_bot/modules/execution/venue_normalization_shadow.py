"""Log-only normalization of bytes already fetched by the legacy REST client."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.external.bitfinex.auth_rest import parse_funding_trades
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.external.bitfinex.observations import (
    TradeObservation,
    parse_credit_observations,
    parse_offer_observations,
    parse_wallet_observations,
)
from bfx_funding_bot.modules.execution.venue_observation import (
    is_funding_wallet,
    normalize_credit,
    normalize_credit_history,
    normalize_offer,
    normalize_offer_history,
    normalize_trade,
    normalize_wallet,
)

log = logging.getLogger("bfx_funding_bot.venue_normalization_shadow")
KEY_CAP = 128
SUMMARY_INTERVAL_S = 3600
SNAPSHOT_MAX_AGE_S = 30
RESIDUAL_TOLERANCE = Decimal("1e-8")


@dataclass
class _ResidualStats:
    samples: int = 0
    max_abs: Decimal = Decimal(0)
    last: Decimal = Decimal(0)
    above_tolerance: int = 0
    stale: int = 0



def classify_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if parts == ["v2", "auth", "r", "wallets"]:
        return "wallets"
    if parts[:4] != ["v2", "auth", "r", "funding"] or len(parts) < 5:
        return None
    kind = parts[4]
    if kind not in {"offers", "credits", "loans", "trades"}:
        return None
    tail = parts[5:]
    history = bool(tail and tail[-1] == "hist")
    symbol = tail[:-1] if history else tail
    if len(symbol) > 1 or (symbol and not symbol[0].startswith("f")):
        return None
    if kind == "trades":
        return "trades" if history else None
    return kind + ("_history" if history else "")


class VenueNormalizationShadow:
    """Bounded process-lifetime dedup; hourly counts use a monotonic call clock.

    This synchronous observer has no authority over legacy results. Its caller
    isolates every exception, including failures in the logging infrastructure.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_summary = clock()
        self._counts: dict[tuple[str, str, str], int] = {}
        self._overflow = 0
        self._snapshots: dict[str, tuple[float, dict[str, Decimal]]] = {}
        self._residuals: dict[str, _ResidualStats] = {}

    def _wallet_residual(
        self, currency: str, balance: Decimal, available: Decimal, now: float,
    ) -> None:
        if currency not in self._residuals:
            if len(self._residuals) >= KEY_CAP:
                return
            self._residuals[currency] = _ResidualStats()
        stats = self._residuals[currency]
        if any(stream not in self._snapshots
               or now - self._snapshots[stream][0] > SNAPSHOT_MAX_AGE_S
               for stream in ("offers", "credits", "loans")):
            stats.stale += 1
            return
        committed = sum((self._snapshots[stream][1].get("f" + currency, Decimal(0))
                         for stream in ("offers", "credits", "loans")), Decimal(0))
        residual = balance - available - committed
        stats.samples += 1
        stats.max_abs = max(stats.max_abs, abs(residual))
        stats.last = residual
        if abs(residual) > RESIDUAL_TOLERANCE:
            stats.above_tolerance += 1
            if stats.above_tolerance == 1:
                log.warning("venue_wallet_residual currency=%s residual=%s", currency, residual)

    def _report(self, stream: str, reason: str, status: str = "", row_id: Any = None) -> None:
        head = status.split(maxsplit=1)[0] if status.strip() else ""
        key = (stream, reason, head)
        if key in self._counts:
            self._counts[key] += 1
        elif len(self._counts) < KEY_CAP:
            self._counts[key] = 1
            log.warning("venue_normalization_rejected stream=%s reason=%s status=%r row_id=%s",
                        stream, reason, status[:200], row_id)
        else:
            self._overflow += 1

    def __call__(self, path: str, content: bytes) -> None:
        now = self._clock()
        if now - self._last_summary >= SUMMARY_INTERVAL_S:
            log.info("venue_normalization_summary counts=%s overflow=%s",
                     dict(self._counts), self._overflow)
            for currency, stats in self._residuals.items():
                log.info(
                    "venue_wallet_residual_summary currency=%s samples=%s max_abs=%s "
                    "last=%s above_tolerance=%s stale=%s",
                    currency, stats.samples, stats.max_abs, stats.last,
                    stats.above_tolerance, stats.stale,
                )
            self._last_summary = now
        stream = classify_path(path)
        if stream is None:
            return
        try:
            rows = json.loads(content, parse_float=Decimal)
        except (ValueError, UnicodeDecodeError):
            self._report(stream, "invalid_json")
            return
        if not isinstance(rows, list):
            self._report(stream, "non_list_body")
            return
        amounts: dict[str, Decimal] = {}
        for row in rows:
            status_index = 10 if stream.startswith("offers") else 7
            status = (str(row[status_index]) if isinstance(row, list)
                      and len(row) > status_index and stream != "trades" else "")
            row_id = row[0] if isinstance(row, list) and row else None
            reason = "parse_error"
            try:
                if stream == "wallets":
                    if isinstance(row, list) and row and not is_funding_wallet(str(row[0])):
                        continue
                    wallet = parse_wallet_observations([row])[0]
                    reason = "normalization_error"
                    normalize_wallet(wallet)
                    if wallet.available is not None:
                        self._wallet_residual(wallet.currency, wallet.balance, wallet.available, now)
                elif stream.startswith("offers"):
                    offer = parse_offer_observations([row])[0]
                    if stream == "offers":
                        amounts[offer.symbol] = amounts.get(offer.symbol, Decimal(0)) + offer.amount
                    reason = "normalization_error"
                    (normalize_offer_history if stream.endswith("_history")
                     else normalize_offer)(offer)
                elif stream.startswith(("credits", "loans")):
                    credit = parse_credit_observations(
                        [row], source_kind="loan" if stream.startswith("loans") else "credit",
                    )[0]
                    if stream in {"credits", "loans"}:
                        amounts[credit.symbol] = amounts.get(credit.symbol, Decimal(0)) + credit.amount
                    reason = "normalization_error"
                    (normalize_credit_history if stream.endswith("_history")
                     else normalize_credit)(credit)
                else:
                    trade = parse_funding_trades([row])[0]
                    reason = "normalization_error"
                    normalize_trade(TradeObservation(
                        trade.trade_id, trade.symbol, trade.mts_create, trade.offer_id,
                        Decimal(str(row[4])).copy_abs(), trade.rate, trade.period_days,
                        trade.maker, tuple(row),
                    ))
            except (BitfinexShapeError, ArithmeticError, TypeError, ValueError, IndexError) as exc:
                # Never log raw rows or exception messages containing their payload.
                if reason == "normalization_error" and str(exc).startswith("unknown "):
                    reason = "unknown_status"
                self._report(stream, reason, status, row_id)

        # Only an all-symbol response is a complete snapshot; a per-symbol
        # read must not replace it with a partial one.
        if stream in {"offers", "credits", "loans"} and path.rstrip("/").split("/")[-1] == stream:
            self._snapshots[stream] = (now, amounts)
