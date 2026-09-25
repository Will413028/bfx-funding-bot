"""Operator trading control: resume and kill; the daemon applies them.

Reached only through the frontend BFF, which requires an MFA-verified operator
session before it mints the JWT this router checks (lending envelope ADR
2026-09-25 D4). The web API holds no write on the trading state or the venue:
it queues an operator request (``execution.operator_requests``) and returns
202; the account daemon re-checks the operator, applies it under the account
lock and records the outcome, which ``GET .../requests/{id}`` and the overview
report. Neither action depends on which build is running.
"""
from __future__ import annotations

import os
import re
import time
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
    FundingCancelAllAuditRow,
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import to_state

_DIGEST = r"^sha256:[0-9a-f]{64}$"
Action = Literal["resume", "kill"]


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


def _request(row: TradingControlRequestRow) -> dict[str, Any]:
    return {
        "request_id": str(row.request_id), "action": row.action, "reason": row.reason,
        "requested_by": row.requested_by, "created_at_ms": row.created_at_ms,
        "state": row.state, "processed_at_ms": row.processed_at_ms,
        "outcome_reason": row.outcome_reason, "trading_state_id": row.trading_state_id,
    }


def _state(row: TradingStateRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    state = to_state(row)
    return {"id": state.id, "state": state.state, "cause": state.cause, "actor": state.actor,
            "reason": state.reason, "at_ms": state.created_at_ms}


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
        requests = (await session.scalars(select(TradingControlRequestRow).where(
            TradingControlRequestRow.exchange_account_id == scope[0],
            TradingControlRequestRow.deployment_environment == scope[1],
        ).order_by(TradingControlRequestRow.created_at_ms.desc()).limit(10))).all()
        return {"data": {
            "trading_state": _state(state),
            "cancel_all": await _cancel_all(session, state),
            "running": _running_identity(),
            "latest_deployment": None if deployment is None else {
                "source_revision": deployment.source_revision,
                "backend_digest": deployment.backend_digest,
                # Terminal rows always have one; only a `started` row has none.
                "finished_at": deployment.finished_at.isoformat() if deployment.finished_at else None,
            },
            "requests": [_request(r) for r in requests],
        }}

    @router.post("/{action}", status_code=status.HTTP_202_ACCEPTED)
    async def request(action: Action, body: ControlBody,
                      context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:  # noqa: B008
        require_account_write(context)
        request_id = uuid4()
        if not await insert_request(session, TradingControlRequestRow, {
            "request_id": request_id, "exchange_account_id": context.exchange_account_id,
            "deployment_environment": context.deployment_environment, "action": action,
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
