"""Pure strategy vocabulary and decision contracts."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy._internal.lend_decision import LendDecision

if TYPE_CHECKING:
    from bfx_funding_bot.modules.strategy.config import CellConfig


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



class Strategy(Protocol):
    """Instance-only strategy port; research construction is a separate contract."""

    @property
    def name(self) -> str: ...
    def observe(self, candle: FundingCandle) -> None: ...
    def decide(self, candle: FundingCandle) -> LendDecision | None: ...
    def diagnostics(self) -> StrategyDiagnostics: ...


class CellStrategyFactory(Protocol):
    def __call__(self, cell: CellConfig) -> Strategy: ...


@dataclass(frozen=True)
class StrategyBuildResult:
    strategy: Strategy
    observed_count: int


class ResearchStrategySpec(Protocol):
    """Research construction and sweeps, independent of live CellConfig.

    name is the legacy class-name report key (not an instance's live name).
    create forwards constructor kwargs unchanged, including the required
    frr_at: Callable[[int], Decimal | None] for the two FRR strategies.
    """

    @property
    def name(self) -> str: ...
    def param_grid_for_cell(
        self, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]: ...
    def create(self, **params: Any) -> Strategy: ...


@dataclass(frozen=True)
class MeanReversionDiagnostics:
    ema_current: Decimal | None
    last_deviation: Decimal | None


@dataclass(frozen=True)
class RatePercentileDiagnostics:
    last_threshold: Decimal | None
    window_filled: bool
    window_values: tuple[Decimal, ...]


@dataclass(frozen=True)
class AdaptivePeriodDiagnostics:
    ema_current: Decimal | None
    window_filled: bool


@dataclass(frozen=True)
class NoStrategyDiagnostics:
    """Stateless baselines expose no reporter state."""


StrategyDiagnostics = (
    MeanReversionDiagnostics | RatePercentileDiagnostics
    | AdaptivePeriodDiagnostics | NoStrategyDiagnostics
)

