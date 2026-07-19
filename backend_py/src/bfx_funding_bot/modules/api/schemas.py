"""Pydantic DTOs for the api-keys endpoints. Response keys are camelCase to match
the FE types (frontend/src/types/index.ts) and the SP1 profile endpoint."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    label: str = ""
    api_key: str = Field(alias="apiKey")
    api_secret: str = Field(alias="apiSecret")


class ApiKeyResponse(BaseModel):
    id: str
    label: str
    api_key: str = Field(serialization_alias="apiKey")
    api_secret: str = Field(default="****", serialization_alias="apiSecret")
    exchange_status: str = Field(serialization_alias="exchangeStatus")
    created_at: str = Field(serialization_alias="createdAt")
    verified_at: str | None = Field(default=None, serialization_alias="verifiedAt")
    last_verify_error: str | None = Field(default=None, serialization_alias="lastVerifyError")


class VerifyResultResponse(BaseModel):
    status: str
    error: str | None = None


class Range(BaseModel):
    """A {min, max} bound. min/max must be positive and min <= max. Used for
    amount, rate (daily ratio) and period. Modelled as float to keep zero
    divergence from the FE's single rangeSchema (which types all three as
    number); period values are integers by convention (FE step=1)."""

    min: float = Field(gt=0)
    max: float = Field(gt=0)

    @model_validator(mode="after")
    def _min_le_max(self) -> Range:
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self


class StrategyConfigBody(BaseModel):
    """Validated PUT /configs body. Mirrors the FE Zod strategyConfigSchema
    (positive ranges, min<=max, non-empty currency). Stored verbatim as the
    user_configs.config JSONB blob. The backend is unit-agnostic on rate; it
    stores the daily ratio the FE sends and never converts."""

    model_config = ConfigDict(populate_by_name=True)

    currency: str = Field(min_length=1)
    amount: Range
    rate: Range
    period: Range
    auto_renew: bool = Field(alias="autoRenew")


class UserConfigResponse(BaseModel):
    id: str
    user_id: str = Field(serialization_alias="userId")
    config: dict[str, object]
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class WeeklyAttributionResponse(BaseModel):
    cell: str
    week_start_ms: int = Field(serialization_alias="weekStartMs")
    week_end_ms: int = Field(serialization_alias="weekEndMs")
    n_fills: int = Field(serialization_alias="nFills")
    gross_interest_usdt: str = Field(serialization_alias="grossInterestUsdt")
    net_interest_usdt: str = Field(serialization_alias="netInterestUsdt")
    capital_days: str = Field(serialization_alias="capitalDays")
    realized_apr_net_pct: str | None = Field(serialization_alias="realizedAprNetPct")
    baseline_close_apr_net_pct: str | None = Field(
        serialization_alias="baselineCloseAprNetPct"
    )
    baseline_frr_apr_net_pct: str | None = Field(
        serialization_alias="baselineFrrAprNetPct"
    )
    baseline_frr_util_apr_net_pct: str | None = Field(
        default=None, serialization_alias="baselineFrrUtilAprNetPct",
    )


class PositionResponse(BaseModel):
    """SP4: one position_state row (per-symbol ledger projection)."""

    symbol: str
    reserved: str
    realized: str
    n_credits: int | None = Field(default=None, serialization_alias="nCredits")
    last_updated_ms: int = Field(serialization_alias="lastUpdatedMs")
    last_reconciled_at: int | None = Field(
        default=None, serialization_alias="lastReconciledAtMs"
    )
    last_event_seq: int = Field(serialization_alias="lastEventSeq")


class OfferClaimResponse(BaseModel):
    """SP4: one offer_claims row (cid-keyed offer FSM projection)."""

    cid: int
    venue_offer_id: str | None = Field(default=None, serialization_alias="venueOfferId")
    state: str
    symbol: str
    size_usdt: str = Field(serialization_alias="sizeUsdt")
    occurred_at_ms: int = Field(serialization_alias="occurredAtMs")
    last_updated_ms: int = Field(serialization_alias="lastUpdatedMs")


class ExecutionEventResponse(BaseModel):
    """SP4: one event_log row (amount/rate lifted from payload when present)."""

    event_seq: int = Field(serialization_alias="eventSeq")
    event_type: str = Field(serialization_alias="eventType")
    occurred_at_ms: int = Field(serialization_alias="occurredAtMs")
    symbol: str | None = None
    venue_offer_id: str | None = Field(default=None, serialization_alias="venueOfferId")
    cid: int | None = None
    amount: str | None = None
    rate: float | None = None
