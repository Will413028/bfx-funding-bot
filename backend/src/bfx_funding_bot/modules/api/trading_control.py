"""Operator trading control: kill and per-currency disable; resume and enabling a currency
go through the operator CLI only (ADR 2026-10-15 D4).

Reached only through the frontend BFF, which requires an MFA-verified operator
session before it mints the JWT this router checks (lending envelope ADR
2026-09-25 D4). The web API holds no write on the trading state or the venue:
it queues an operator request (``execution.operator_requests``) and returns
202; the account daemon re-checks the operator, applies it under the account
lock and records the outcome, which ``GET .../requests/{id}`` and the overview
report -- the CLI's resume and enable requests included. No action depends on which
build is running. The checks and the queueing are ``execution.operator_commands``,
shared with the operator CLI; this router maps their refusals to HTTP. The database
refuses the web API's role a resume or an enable too (a trigger, 36e59526f8cf).

The per-currency ``enabled`` flag of the applied CapitalPolicy is the everyday
stop (ADR D4). Its requests go to their own outbox (``capital_policy_requests``,
applied by ``CapitalPolicyRequestWorker``), so a kill never shares a queue or a
pending slot with them. The overview lists every currency with an applied
policy: enabled, the envelope (or that it is unset) and its latest requests.
"""
from __future__ import annotations

import os
import re
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    command_error,
    require_account_member,
)
from bfx_funding_bot.modules.api.deps import ReadModels, get_read_models, get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import Envelope, ResponseModel
from bfx_funding_bot.modules.deployments.tables import DeploymentRow
from bfx_funding_bot.modules.execution import operator_commands as commands
from bfx_funding_bot.modules.execution.operator_commands import (
    CONTROL_REASON_MAX,
    SYMBOL_PATTERN,
    CurrencyAction,
    OperatorCommandError,
    TradingAction,
)
from bfx_funding_bot.modules.execution.operator_requests import RequestState

_DIGEST = r"^sha256:[0-9a-f]{64}$"


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=CONTROL_REASON_MAX)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, reason: str) -> str:
        # The table refuses a blank reason too; refused here, it is a 422, not a database error.
        if not reason.strip():
            raise ValueError("reason must not be blank")
        return reason


# The responses keep the snake_case of ``execution.operator_commands``' views, which the
# operator CLI prints too. A closed value set is the domain's type where one exists; these
# three have only a constant or a CHECK constraint, and tests/modules/api/
# test_literal_sources.py (constants) and tests/integration/test_api_literal_checks.py
# (the migrated CHECKs) fail when either side changes alone.
TradingStateValue = Literal["ACTIVE", "HALTED"]  # trading_state.STATES, ck_trading_state_state
TradingStateCause = Literal["operator", "auto"]  # trading_state.CAUSES, ck_trading_state_cause
# ck_funding_cancel_all_audit_phase
CancelAllPhaseValue = Literal["requested", "acknowledged", "rejected", "failed", "skipped"]


class TradingStateView(ResponseModel):
    id: int
    state: TradingStateValue
    cause: TradingStateCause
    actor: str
    reason: str
    at_ms: int


class CancelAllPhase(ResponseModel):
    """The latest venue cancel-all phase of one currency for the HALTED in force."""

    currency: str
    phase: CancelAllPhaseValue
    detail: str | None
    at_ms: int


class BuildIdentity(ResponseModel):
    backend_digest: str | None
    source_revision: str | None


class DeployedBuild(BuildIdentity):
    # Terminal rows always have one; only a `started` row has none.
    finished_at: str | None


class TradingControlRequest(ResponseModel):
    request_id: str
    action: TradingAction
    reason: str
    requested_by: str
    created_at_ms: int
    state: RequestState
    processed_at_ms: int | None
    outcome_reason: str | None


class CurrencyRequest(ResponseModel):
    request_id: str
    symbol: str
    action: CurrencyAction
    reason: str
    requested_by: str
    created_at_ms: int
    state: RequestState
    processed_at_ms: int | None
    outcome_reason: str | None


class OfferEnvelope(ResponseModel):
    """The terms every new offer must stay inside; decimals stay strings."""

    min_period_days: int
    max_period_days: int
    max_open_offers: int
    rate_floor_ratio: str
    min_rate_apr: str


class CurrencyPolicy(ResponseModel):
    """A currency with an applied policy; an unreadable one has ``policy_error`` and nulls."""

    symbol: str
    revision: int
    policy_error: str | None
    enabled: bool | None
    max_offer_amount: str | None
    envelope: OfferEnvelope | None
    requests: list[CurrencyRequest]


class TradingControlOverview(ResponseModel):
    trading_state: TradingStateView | None
    cancel_all: list[CancelAllPhase]
    running: BuildIdentity
    latest_deployment: DeployedBuild | None
    requests: list[TradingControlRequest]
    currencies: list[CurrencyPolicy]


class KillRequested(ResponseModel):
    request_id: str
    action: Literal["kill"]
    state: Literal["requested"]


class CurrencyDisableRequested(ResponseModel):
    request_id: str
    symbol: str
    action: Literal["disable"]
    state: Literal["requested"]


def _running_identity() -> BuildIdentity:
    """The backend image this web API runs -- the same image as the daemon."""
    digest = os.environ.get("BFX_IMAGE_DIGEST", "").strip()
    return BuildIdentity(
        backend_digest=digest if re.fullmatch(_DIGEST, digest) else None,
        source_revision=os.environ.get("BFX_SOURCE_REVISION", "").strip() or None,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_trading_control_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/exchange-accounts/{exchange_account_id}/trading-control",
                       tags=["trading-control"], dependencies=[Depends(shared_rate_limit_dependency())])

    @router.get("")
    async def overview(context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                       session: AsyncSession = Depends(get_session),  # noqa: B008
                       models: ReadModels = Depends(get_read_models),  # noqa: B008
                       ) -> Envelope[TradingControlOverview]:
        view = await commands.trading_overview(session, context,
                                               models.policy_store(context.scope))
        deployment = await session.scalar(select(DeploymentRow).where(
            DeploymentRow.outcome == "deployed").order_by(DeploymentRow.id.desc()).limit(1))
        return Envelope(data=TradingControlOverview(
            trading_state=view["trading_state"],
            cancel_all=view["cancel_all"],
            running=_running_identity(),
            latest_deployment=None if deployment is None else DeployedBuild(
                source_revision=deployment.source_revision,
                backend_digest=deployment.backend_digest,
                finished_at=deployment.finished_at.isoformat() if deployment.finished_at else None,
            ),
            requests=view["requests"],
            currencies=view["currencies"],
        ))

    @router.post("/kill", status_code=202)
    async def request_kill(body: ControlBody,
                           context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                           session: AsyncSession = Depends(get_session),  # noqa: B008
                           ) -> Envelope[KillRequested]:
        try:
            request_id = await commands.request_trading_control(
                session, context, action="kill", reason=body.reason, now_ms=_now_ms())
        except OperatorCommandError as exc:
            raise command_error(exc, context=context) from exc
        return Envelope(data=KillRequested(request_id=str(request_id), action="kill",
                                           state="requested"))

    @router.post("/currencies/{symbol}/disable", status_code=202)
    async def request_currency_disable(
            body: ControlBody,
            symbol: str = Path(pattern=SYMBOL_PATTERN),
            context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
            session: AsyncSession = Depends(get_session),  # noqa: B008
            ) -> Envelope[CurrencyDisableRequested]:
        try:
            request_id = await commands.request_currency(
                session, context, symbol=symbol, action="disable", reason=body.reason,
                now_ms=_now_ms())
        except OperatorCommandError as exc:
            raise command_error(exc, context=context) from exc
        return Envelope(data=CurrencyDisableRequested(
            request_id=str(request_id), symbol=symbol, action="disable", state="requested"))

    @router.get("/currency-requests/{request_id}")
    async def get_currency_request(
            request_id: UUID,
            context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
            session: AsyncSession = Depends(get_session),  # noqa: B008
            ) -> Envelope[CurrencyRequest]:
        try:
            row = await commands.get_currency_request(session, context, request_id)
        except OperatorCommandError as exc:
            raise command_error(exc, context=context) from exc
        return Envelope(data=CurrencyRequest.model_validate(commands.currency_request_view(row)))

    @router.get("/requests/{request_id}")
    async def get_request(request_id: UUID,
                          context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                          session: AsyncSession = Depends(get_session),  # noqa: B008
                          ) -> Envelope[TradingControlRequest]:
        try:
            row = await commands.get_trading_request(session, context, request_id)
        except OperatorCommandError as exc:
            raise command_error(exc, context=context) from exc
        return Envelope(data=TradingControlRequest.model_validate(commands.trading_request_view(row)))

    return router
