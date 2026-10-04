"""Marketfeed daemon configuration model."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.core.venue import Venue, venue_for_phase
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment
from bfx_funding_bot.modules.strategy import CellConfig


class MarketfeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Annotated[Phase, Field(description="shadow / live")]
    # Always equal to venue_for_phase(phase); load_config sets it, and a hand-built
    # config that omits it gets the derived value.
    venue: Venue
    # Simulated venue only: wallet currency -> amount funded when the venue log is empty.
    simulated_initial_wallets: dict[str, Decimal] = Field(default_factory=dict)
    cells: list[CellConfig]
    database_url: str = Field(repr=False)
    deployment_environment: DeploymentEnvironment
    execution_policy: ExecutionPolicy
    book_max_age_seconds: float | None = Field(default=None, gt=0)
    book_reconcile_interval_seconds: float | None = Field(default=None, gt=0)
    book_max_down_pct: float | None = Field(default=None, ge=0, le=1)
    optimizer_fee_rate: Decimal | None = Field(default=None, ge=0, le=1)
    fill_model_artifact: str | None = None
    redis_url: str | None = Field(default=None, repr=False)
    run_duration_hours: int | None = Field(default=None, gt=0)
    # Bug C fix (5/20): scheduler observe-after-close buffer. Was 5s
    # default — but Bitfinex p30 candles sometimes land in DB > 5s after
    # hh:00 → scheduler reads 0 rows → mis-emits health degraded (Bug A).
    # 30s is the new default; calibration period (Phase 4.3) tunes via
    # BFX_SCHEDULER_BUFFER_S env override.
    scheduler_buffer_s: float = Field(default=30.0, gt=0)
    # Phase 4.3 LOCF: global default staleness budget for LOCF fill.
    # p30 sparse cells override per-entry in cells.yaml (12h).
    # Override via BFX_STALENESS_BUDGET_HOURS_DEFAULT env var.
    staleness_budget_hours_default: int = Field(default=2, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _derive_venue(cls, data: object) -> object:
        if isinstance(data, dict) and "venue" not in data and "phase" in data:
            return {**data, "venue": venue_for_phase(Phase(data["phase"]))}
        return data

    @model_validator(mode="after")
    def _venue_follows_phase(self) -> MarketfeedConfig:
        if self.venue != venue_for_phase(self.phase):
            raise ValueError(f"venue {self.venue!r} does not match phase {self.phase.value!r}")
        return self
