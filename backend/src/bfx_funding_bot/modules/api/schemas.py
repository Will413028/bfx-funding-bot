"""Pydantic DTOs of the /api/v1 web API: the single source of its contract (9k).

Every JSON route declares its response model, so ``app.openapi()`` describes the wire
format and the frontend types are generated from it (``scripts/dump_openapi.py``, then
``pnpm gen:api`` in ``frontend/``). Most response keys are camelCase via
``serialization_alias``; the trading-control and funding-status payloads stay snake_case.
A field with a closed set of values is a ``Literal``, so the generated type keeps the
union and a value outside it fails the response (500) instead of reaching the console.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CredentialStatus = Literal["pending", "verified", "unverified", "failed", "revoked", "retired"]
OfferClaimState = Literal["pending", "unknown", "claimed", "released", "failed"]


class ResponseModel(BaseModel):
    """A response DTO. A field with a default is still always sent, so the schema marks it
    required (the generated type is ``T | null``, not ``T | null | undefined``)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Envelope[T](ResponseModel):
    """The ``{"data": ...}`` envelope every JSON route answers with."""

    data: T


class ProfileResponse(ResponseModel):
    """The operator identity Tailscale Serve forwards."""

    user_id: str = Field(serialization_alias="userId")
    email: str | None
    role: str


class ApiKeyResponse(BaseModel):
    id: str
    label: str
    api_key: str = Field(serialization_alias="apiKey")
    api_secret: str = Field(default="****", serialization_alias="apiSecret")
    exchange_status: str = Field(serialization_alias="exchangeStatus")
    created_at: str = Field(serialization_alias="createdAt")
    verified_at: str | None = Field(default=None, serialization_alias="verifiedAt")
    last_verify_error: str | None = Field(default=None, serialization_alias="lastVerifyError")


class ExchangeAccountResponse(ResponseModel):
    """One account visible to the authenticated operator bootstrap (never a retired one)."""

    exchange_account_id: str = Field(serialization_alias="exchangeAccountId")
    venue: str
    label: str
    lifecycle_status: Literal["active", "halted"] = Field(serialization_alias="lifecycleStatus")
    role: Literal["owner", "operator", "viewer"]


class AccountCredentialResponse(ResponseModel):
    """Masked account-owned credential lifecycle record."""

    id: str
    exchange_account_id: str = Field(serialization_alias="exchangeAccountId")
    label: str
    api_key: str = Field(serialization_alias="apiKey")
    api_secret: str = Field(default="****", serialization_alias="apiSecret")
    status: CredentialStatus
    created_at: str = Field(serialization_alias="createdAt")
    verified_at: str | None = Field(default=None, serialization_alias="verifiedAt")
    last_verify_error: str | None = Field(default=None, serialization_alias="lastVerifyError")


class AccountConfigDraftResponse(ResponseModel):
    """Explicitly account-scoped, not-yet-applied strategy configuration.

    ``config`` is the stored ``StrategyConfigBody`` (the PUT is its only writer)."""

    id: str
    exchange_account_id: str = Field(serialization_alias="exchangeAccountId")
    config: StrategyConfigBody
    revision: int
    source: str
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


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


class WeeklyAttributionResponse(ResponseModel):
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


class PublicWeeklyPoint(ResponseModel):
    """One week of the public proof-page series — percent-only, no absolute
    $ fields (see modules/api/public.py for the compliance rationale)."""

    week_start_ms: int = Field(serialization_alias="weekStartMs")
    realized_apr_net_pct: str | None = Field(serialization_alias="realizedAprNetPct")
    baseline_frr_util_apr_net_pct: str | None = Field(
        serialization_alias="baselineFrrUtilAprNetPct"
    )


class PublicProofSummaryResponse(ResponseModel):
    weeks: list[PublicWeeklyPoint]
    as_of: str = Field(serialization_alias="asOf")


class PositionResponse(ResponseModel):
    """One symbol's capital as disjoint components, in the symbol's own units.

    ``available + offered + lent`` is the symbol's total. ``unattributedLent`` is a
    subset of ``lent`` (never an addend); null when the authority has no such fact.
    """

    symbol: str
    available: str
    offered: str
    lent: str
    unattributed_lent: str | None = Field(default=None, serialization_alias="unattributedLent")
    n_credits: int | None = Field(default=None, serialization_alias="nCredits")
    last_updated_ms: int = Field(serialization_alias="lastUpdatedMs")
    last_reconciled_at: int | None = Field(
        default=None, serialization_alias="lastReconciledAtMs"
    )


class OfferClaimResponse(ResponseModel):
    """One managed offer. ``offerKey`` is opaque and stable for the offer's life."""

    offer_key: str = Field(serialization_alias="offerKey")
    venue_offer_id: str | None = Field(default=None, serialization_alias="venueOfferId")
    state: OfferClaimState
    symbol: str
    size_usdt: str = Field(serialization_alias="sizeUsdt")
    occurred_at_ms: int = Field(serialization_alias="occurredAtMs")
    last_updated_ms: int = Field(serialization_alias="lastUpdatedMs")


class ExecutionEventResponse(ResponseModel):
    """One execution-history row: a ledger journal row, or below the switch a row of the frozen
    ``legacy_archive.event_log``. ``eventKey`` is opaque (ADR 2026-10-02 D4)."""

    event_key: str = Field(serialization_alias="eventKey")
    event_type: str = Field(serialization_alias="eventType")
    occurred_at_ms: int = Field(serialization_alias="occurredAtMs")
    symbol: str | None = None
    venue_offer_id: str | None = Field(default=None, serialization_alias="venueOfferId")
    cid: int | None = None
    amount: str | None = None
    rate: float | None = None


class ExecutionsPagination(ResponseModel):
    has_more: bool = Field(serialization_alias="hasMore")
    next_before: str | None = Field(serialization_alias="nextBefore")


class ExecutionsPage(Envelope[list[ExecutionEventResponse]]):
    """``GET /executions``: the envelope plus the opaque cursor of the next page."""

    pagination: ExecutionsPagination
