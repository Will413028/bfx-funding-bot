"""Exact-period market pricing for deployment candidates."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from bfx_funding_bot.modules.execution.contracts import BlockedExecution, BlockReason
from bfx_funding_bot.modules.marketfeed.funding_book import MarketSnapshot
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload


class PriceBranch(StrEnum):
    TAKER = "taker"
    UNDERCUT = "undercut"
    RAISE = "raise"
    SIGNAL_FLOOR = "signal_floor"


@dataclass(frozen=True, slots=True)
class PriceDecision:
    rate: Decimal
    branch: PriceBranch
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PeriodPricer:
    max_down_pct: Decimal
    tick: Decimal

    def price(
        self,
        *,
        candidate: DecisionPayload,
        snapshot: MarketSnapshot,
    ) -> PriceDecision | BlockedExecution:
        signal_rate = _required_decimal(candidate.offer_rate, "offer_rate")
        amount = _required_decimal(candidate.offer_amount_usdt, "offer_amount_usdt")
        period_days = candidate.offer_duration_days
        if period_days is None:
            raise ValueError("POST candidate requires offer_duration_days")

        bids = tuple(level for level in snapshot.bids if level.period == period_days)
        asks = tuple(level for level in snapshot.asks if level.period == period_days)
        if not bids and not asks:
            return _blocked(candidate, BlockReason.PERIOD_NOT_FOUND, "exact_period")

        taker_bids = [
            level
            for level in bids
            if _decimal(level.rate) >= signal_rate and abs(_decimal(level.amount)) >= amount
        ]
        if taker_bids:
            bid = max(taker_bids, key=lambda level: level.rate)
            return PriceDecision(
                rate=signal_rate,
                branch=PriceBranch.TAKER,
                evidence={
                    "period_days": period_days,
                    "bid_rate": str(_decimal(bid.rate)),
                    "bid_amount": str(abs(_decimal(bid.amount))),
                    "snapshot_id": snapshot.snapshot_id,
                },
            )

        eligible_asks = [
            level for level in asks if _decimal(level.amount) >= amount
        ]
        if not eligible_asks:
            return _blocked(candidate, BlockReason.INSUFFICIENT_PERIOD_DEPTH, "exact_period_ask")

        ask = min(eligible_asks, key=lambda level: level.rate)
        competitive = _decimal(ask.rate) - self.tick
        floor = signal_rate * (Decimal("1") - self.max_down_pct)
        evidence = {
            "period_days": period_days,
            "ask_rate": str(_decimal(ask.rate)),
            "ask_amount": str(_decimal(ask.amount)),
            "competitive_rate": str(competitive),
            "signal_floor": str(floor),
            "snapshot_id": snapshot.snapshot_id,
        }
        if competitive < floor:
            return PriceDecision(signal_rate, PriceBranch.SIGNAL_FLOOR, evidence)
        if competitive >= signal_rate:
            return PriceDecision(competitive, PriceBranch.UNDERCUT, evidence)
        return PriceDecision(competitive, PriceBranch.RAISE, evidence)


def _blocked(
    candidate: DecisionPayload,
    reason: BlockReason,
    dependency: str,
) -> BlockedExecution:
    return BlockedExecution(
        decision_id=str(candidate.signal_correlation_id),
        candidate=candidate,
        reason=reason,
        failed_dependency=dependency,
        evidence={"symbol": candidate.symbol, "period_days": candidate.offer_duration_days},
    )


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _required_decimal(value: float | None, name: str) -> Decimal:
    if value is None:
        raise ValueError(f"POST candidate requires {name}")
    return _decimal(value)
