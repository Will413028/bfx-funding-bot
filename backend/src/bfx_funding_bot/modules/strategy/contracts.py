"""Pure strategy vocabulary and decision contracts."""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrategyName(StrEnum):
    RATE_PERCENTILE = "rate_percentile"
    MEAN_REVERSION = "mean_reversion"
    ADAPTIVE_PERIOD = "adaptive_period"



class SignalDirection(StrEnum):
    POST = "post"
    SKIP = "skip"



class DecisionOutcome(StrEnum):
    POST = "post"
    SKIP = "skip"



class SkipReason(StrEnum):
    BELOW_THRESHOLD = "below_threshold"
    MAX_POSITION_CAP = "max_position_cap"
    SAFETY_BLOCK = "safety_block"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    OTHER = "other"



class SignalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_score: float
    signal_direction: SignalDirection
    strategy_attributes: dict[str, Any] = Field(..., min_length=1)
    divergence_detail: dict[str, Any] | None = None
    # ── NEW (Phase 4.3 LOCF) ──
    is_stale: bool = False
    stale_seconds: int = 0
    budget_seconds: int  # required: every signal carries its cell's budget



class DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_outcome: DecisionOutcome
    signal_correlation_id: UUID
    # Money terms are Decimal end-to-end (planner -> intent/attempt -> command
    # gate -> venue body): the amount's last 4 of 8 decimals are the submit's
    # only venue identity (D3a fingerprint), so no float may sit on this path.
    # JSON dumps (audit, stdout, safety snapshots) carry them as exact strings.
    offer_rate: Decimal | None = None
    offer_amount_usdt: Decimal | None = None
    offer_duration_days: int | None = None
    skip_reason: SkipReason | None = None
    skip_reason_detail: str | None = None
    # ── Phase 1 per-symbol: the offer currency this decision targets. ──
    # Guards read decision.symbol to pick the per-symbol ledger bucket.
    # MANDATORY (Task 11 — no default, so a decision can never silently land as
    # the legacy "fUSD"); every producer must set the cell's real symbol.
    symbol: str
    # ── NEW (Phase 4.3 LOCF): staleness dimension on the DURABLE decision record ──
    # SIGNAL carries this too but lands on the ephemeral stdout sink; persisting it
    # here (PG diagnostics) lets canary outcomes be sliced stale-vs-fresh via SQL.
    is_stale: bool = False
    stale_seconds: int = 0
    budget_seconds: int | None = None

    @model_validator(mode="after")
    def _check_outcome_fields(self) -> DecisionPayload:
        if self.decision_outcome == DecisionOutcome.POST:
            missing = [
                f for f in ("offer_rate", "offer_amount_usdt", "offer_duration_days")
                if getattr(self, f) is None
            ]
            if missing:
                raise ValueError(f"post decision requires {missing}")
        if self.decision_outcome == DecisionOutcome.SKIP and self.skip_reason is None:
            raise ValueError("skip decision requires skip_reason")
        return self

