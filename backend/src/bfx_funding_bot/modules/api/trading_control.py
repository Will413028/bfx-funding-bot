"""Operator trading control: approve, resume, pause and kill; the daemon applies them.

Reached only through the frontend BFF, which requires an MFA-verified operator
session before it mints the JWT this router checks (ADR 2026-09-25 D2/D4). The
web API holds no write on the trading state, the approvals or the venue (ADR
D4'): it queues an operator request (``execution.operator_requests``) and
returns 202; the account daemon re-checks the operator, applies it under the
account lock and records the outcome, which ``GET .../requests/{id}`` and the
overview report. Approve and resume name the build the operator saw; a stop
(pause, kill) names none and is never refused for it.
"""
from __future__ import annotations

import os
import re
import time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    require_account_member,
    require_account_write,
)
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.deployments.tables import DeploymentRow
from bfx_funding_bot.modules.execution.operator_requests import insert_request
from bfx_funding_bot.modules.execution.safety.tables import (
    DeploymentApprovalRow,
    FundingCancelAllAuditRow,
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import to_state
from bfx_funding_bot.modules.execution.trading_control import probation_progress

_DIGEST = r"^sha256:[0-9a-f]{64}$"
_BUILD_ACTIONS = frozenset({"approve", "resume"})
Action = Literal["approve", "resume", "pause", "kill"]


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Approve/resume: the build the operator saw; the daemon refuses another.
    backend_digest: str | None = Field(default=None, pattern=_DIGEST)
    reason: str = Field(min_length=1, max_length=500)


def _request(row: TradingControlRequestRow) -> dict[str, Any]:
    return {
        "request_id": str(row.request_id), "action": row.action,
        "backend_digest": row.backend_digest, "reason": row.reason,
        "requested_by": row.requested_by, "created_at_ms": row.created_at_ms,
        "state": row.state, "processed_at_ms": row.processed_at_ms,
        "outcome_reason": row.outcome_reason, "trading_state_id": row.trading_state_id,
    }


def _decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


async def _state(session: AsyncSession, row: TradingStateRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    state = to_state(row)
    probation = None
    if state.probation is not None:
        progress = await probation_progress(
            session, account_id=row.exchange_account_id, environment=row.deployment_environment,
            probation=state.probation, now_ms=int(time.time() * 1000))
        probation = {
            "multiplier": _decimal(state.probation.multiplier),
            "started_at_ms": state.probation.started_at_ms,
            "floor": {symbol: _decimal(amount) for symbol, amount in state.probation.floor},
            "elapsed_ms": progress.elapsed_ms, "required_ms": progress.required_ms,
            "acknowledged": progress.acknowledged,
            "required_acknowledged": progress.required_acknowledged,
        }
    return {
        "id": state.id, "state": state.state, "cause": state.cause, "actor": state.actor,
        "reason": state.reason, "at_ms": state.created_at_ms, "probation": probation,
    }


async def _cancel_all(session: AsyncSession, row: TradingStateRow | None) -> list[dict[str, Any]]:
    """The venue cancel-all recorded for the state in force (only a HALTED has
    any): the latest phase per currency. A lone ``requested`` means the call's
    outcome was never recorded -- in flight, or interrupted."""
    if row is None:
        return []
    audits = (await session.scalars(select(FundingCancelAllAuditRow).where(
        FundingCancelAllAuditRow.trading_state_id == row.id,
    ).order_by(FundingCancelAllAuditRow.id))).all()
    latest: dict[str, FundingCancelAllAuditRow] = {}
    for audit in audits:
        latest[audit.currency] = audit
    return [{"currency": a.currency, "phase": a.phase, "detail": a.detail,
             "at_ms": a.occurred_at_ms} for a in sorted(latest.values(), key=lambda a: a.currency)]


def _running_identity() -> dict[str, str | None]:
    """The backend image this web API runs -- the same image as the daemon."""
    digest = os.environ.get("BFX_IMAGE_DIGEST", "").strip()
    return {
        "backend_digest": digest if re.fullmatch(_DIGEST, digest) else None,
        "source_revision": os.environ.get("BFX_SOURCE_REVISION", "").strip() or None,
        "change_class": os.environ.get("BFX_CHANGE_CLASS", "").strip() or None,
    }


def build_trading_control_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/exchange-accounts/{exchange_account_id}/trading-control",
                       tags=["trading-control"], dependencies=[Depends(shared_rate_limit_dependency())])

    @router.get("")
    async def overview(context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:  # noqa: B008
        scope = (context.exchange_account_id, context.deployment_environment)
        state = await session.scalar(select(TradingStateRow).where(
            TradingStateRow.exchange_account_id == scope[0],
            TradingStateRow.deployment_environment == scope[1],
        ).order_by(TradingStateRow.id.desc()).limit(1))
        deployment = await session.scalar(select(DeploymentRow).where(
            DeploymentRow.outcome == "deployed").order_by(DeploymentRow.id.desc()).limit(1))
        approvals = (await session.scalars(select(DeploymentApprovalRow).where(
            DeploymentApprovalRow.exchange_account_id == scope[0],
            DeploymentApprovalRow.deployment_environment == scope[1],
        ).order_by(DeploymentApprovalRow.id.desc()).limit(10))).all()
        requests = (await session.scalars(select(TradingControlRequestRow).where(
            TradingControlRequestRow.exchange_account_id == scope[0],
            TradingControlRequestRow.deployment_environment == scope[1],
        ).order_by(TradingControlRequestRow.created_at_ms.desc()).limit(10))).all()
        return {"data": {
            "trading_state": await _state(session, state),
            "cancel_all": await _cancel_all(session, state),
            "running": _running_identity(),
            "latest_deployment": None if deployment is None else {
                "source_revision": deployment.source_revision,
                "backend_digest": deployment.backend_digest,
                "change_class": deployment.change_class,
                "finished_at": deployment.finished_at.isoformat(),
            },
            "approvals": [{"backend_digest": a.backend_digest, "source_revision": a.source_revision,
                           "approved_by": a.approved_by, "approved_at_ms": a.approved_at_ms}
                          for a in approvals],
            "requests": [_request(r) for r in requests],
        }}

    @router.post("/{action}", status_code=status.HTTP_202_ACCEPTED)
    async def request(action: Action, body: ControlBody,
                      context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:  # noqa: B008
        require_account_write(context)
        if action in _BUILD_ACTIONS and body.backend_digest is None:
            raise HTTPException(status_code=422, detail="backend_digest_required")
        request_id = uuid4()
        if not await insert_request(session, TradingControlRequestRow, {
            "request_id": request_id, "exchange_account_id": context.exchange_account_id,
            "deployment_environment": context.deployment_environment, "action": action,
            # A stop names no build: it is never refused for which one is running.
            "backend_digest": body.backend_digest if action in _BUILD_ACTIONS else None,
            "reason": body.reason, "requested_by": context.user_id,
            "created_at_ms": int(time.time() * 1000),
        }):
            raise HTTPException(status_code=409, detail="request_pending")
        return {"data": {"request_id": str(request_id), "action": action, "state": "requested"}}

    @router.get("/requests/{request_id}")
    async def get_request(request_id: UUID,
                          context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:  # noqa: B008
        row = await session.scalar(select(TradingControlRequestRow).where(
            TradingControlRequestRow.request_id == request_id,
            TradingControlRequestRow.exchange_account_id == context.exchange_account_id,
            TradingControlRequestRow.deployment_environment == context.deployment_environment,
        ))
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return {"data": _request(row)}

    return router
