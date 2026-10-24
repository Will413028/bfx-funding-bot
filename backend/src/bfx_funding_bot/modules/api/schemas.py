"""Pydantic DTOs of the /api/v1 web API: the single source of its contract (9k).

Every JSON route declares its response model, so ``app.openapi()`` describes the wire
format and the frontend types are generated from it (``scripts/dump_openapi.py``, then
``pnpm gen:api`` in ``frontend/``). Most response keys are camelCase via
``serialization_alias``; the trading-control and funding-status payloads stay snake_case.
A field with a closed set of values is a ``Literal``, so the generated type keeps the
union and a value outside it fails the response (500) instead of reaching the console.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bfx_funding_bot.core.auth import LOGIN_HEADER
from bfx_funding_bot.core.security_log import INPUT_REJECTED, clip, security_event
from bfx_funding_bot.modules.accounts.exchange_accounts import AccountRole
from bfx_funding_bot.modules.ledger import OfferState

_logger = logging.getLogger(__name__)

# Closed value sets with no domain type of their own; tests/modules/api/
# test_literal_sources.py pins each to the domain type it narrows, so a value added there
# fails until it is added here.
# CredentialLifecycleStatus without "active", which api_keys._credential_status splits.
CredentialStatus = Literal["pending", "verified", "unverified", "failed", "revoked", "retired"]
# AccountLifecycleStatus without "retired", which the account list never returns.
ListedAccountStatus = Literal["active", "halted"]


def field_paths(exc: ValidationError | ResponseValidationError | RequestValidationError) -> list[str]:
    """Where a payload failed its model, as dotted field paths: loggable, unlike the values
    pydantic's own message repeats."""
    return [".".join(map(str, error["loc"])) for error in exc.errors()]


def install_response_shape_handler(app: FastAPI) -> None:
    """A response model a handler cannot build (a value outside a ``Literal``, a missing
    field), or FastAPI's own check of the returned model, is a 500 logged by route, model and
    field path, never with the values: without this, Starlette logs the traceback, whose
    message repeats every input. Request bodies are FastAPI's ``RequestValidationError``
    (422), neither class."""

    async def refuse(request: Request, exc: Exception) -> JSONResponse:
        # Registered for these two only; Starlette types the handler for any exception.
        fields = (field_paths(exc) if isinstance(exc, ValidationError | ResponseValidationError)
                  else [])
        _logger.error("response_shape_mismatch route=%s model=%s fields=%s",
                      getattr(request.scope.get("route"), "path", "?"),
                      getattr(exc, "title", type(exc).__name__), fields)
        return JSONResponse({"detail": "response_shape_mismatch"}, status_code=500)

    app.add_exception_handler(ValidationError, refuse)
    app.add_exception_handler(ResponseValidationError, refuse)


def install_request_validation_log(app: FastAPI) -> None:
    """A request that fails its model (422) is a security event (ASVS V16.3.3), logged with
    the field paths that failed and never the values; the answer is FastAPI's own 422."""

    async def logged(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, RequestValidationError):  # registered for this class only
            raise exc
        security_event(INPUT_REJECTED, reason="request_validation", method=request.method,
                       path=clip(request.url.path), login=clip(request.headers.get(LOGIN_HEADER)),
                       fields=[clip(path) for path in field_paths(exc)[:20]])
        return await request_validation_exception_handler(request, exc)

    app.add_exception_handler(RequestValidationError, logged)


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
    lifecycle_status: ListedAccountStatus = Field(serialization_alias="lifecycleStatus")
    role: AccountRole


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

    ``config`` is the stored ``StrategyConfigBody`` (the PUT is its only writer). A draft
    stored under another schema is still listed, with ``config`` null and ``configError``
    set, so the console can show it exists and overwrite it."""

    id: str
    exchange_account_id: str = Field(serialization_alias="exchangeAccountId")
    config: StrategyConfigBody | None
    config_error: Literal["config_unreadable"] | None = Field(serialization_alias="configError")
    revision: int
    source: str
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class Range(BaseModel):
    """A {min, max} bound. min/max must be positive and min <= max. Used for
    amount, rate (daily ratio) and period. Modelled as float to keep zero
    divergence from the FE's single rangeSchema (which types all three as
    number); period values are integers by convention (FE step=1). Finite only:
    JSON parsing accepts `Infinity`, `NaN` and `1e999`."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    min: float = Field(gt=0)
    max: float = Field(gt=0)

    @model_validator(mode="after")
    def _min_le_max(self) -> Range:
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self


class StrategyConfigBody(BaseModel):
    """Validated PUT /configs body. Mirrors the FE Zod strategyConfigSchema
    (positive finite ranges, min<=max, alphanumeric currency). Stored verbatim as the
    user_configs.config JSONB blob. The backend is unit-agnostic on rate; it
    stores the daily ratio the FE sends and never converts. Unknown keys are refused,
    and the currency is a short alphanumeric code (the form sends `USD` or a funding
    symbol such as `fUST`)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    currency: str = Field(pattern=r"^[A-Za-z0-9]{1,16}$")
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
    state: OfferState
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
