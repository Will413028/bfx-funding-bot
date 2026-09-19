"""Authenticated human requests; the daemon alone applies release transitions."""
import time
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    require_account_member,
    require_account_write,
)
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.execution.release_session import ReleaseBlocked, ReleaseSessions
from bfx_funding_bot.modules.execution.release_tables import ReleaseSessionRow


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: Literal["fUST"]
    cell: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    max_amount: Decimal = Field(gt=0, allow_inf_nan=False)
    expires_at_ms: int


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


def _data(row: ReleaseSessionRow) -> dict[str, object]:
    return {"data": {
        "id": str(row.id), "state": row.state, "symbol": row.symbol,
        "cell": row.cell, "strategy": row.strategy, "max_amount": str(row.max_amount),
        "expires_at_ms": row.expires_at_ms, "binding": row.binding, "halt_id": row.halt_id,
        "minimum_amount": str(row.minimum_amount) if row.minimum_amount is not None else None,
        "exact_amount": str(row.exact_amount) if row.exact_amount is not None else None,
        "request_revision": row.request_revision, "processed_revision": row.processed_revision,
        "reason": row.reason, "evidence": row.evidence,
    }}


def _error(exc: ReleaseBlocked) -> HTTPException:
    return HTTPException(status_code=404 if str(exc) == "session_not_found" else 409, detail=str(exc))


def build_release_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/exchange-accounts/{exchange_account_id}/release-sessions",
                       tags=["release"], dependencies=[Depends(shared_rate_limit_dependency())])

    @router.post("")
    async def prepare(body: PrepareBody,
                      context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                      session: AsyncSession = Depends(get_session)) -> dict[str, object]:  # noqa: B008
        require_account_write(context)
        repo = ReleaseSessions(context.exchange_account_id, context.deployment_environment)
        try:
            return _data(await repo.request(session, operator=context.user_id,
                **body.model_dump(), now_ms=int(time.time() * 1000)))
        except ReleaseBlocked as exc:
            raise _error(exc) from exc

    @router.get("/{session_id}")
    async def get(session_id: UUID,
                  context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                  session: AsyncSession = Depends(get_session)) -> dict[str, object]:  # noqa: B008
        try:
            return _data(await ReleaseSessions(context.exchange_account_id,
                                               context.deployment_environment).get(session, session_id))
        except ReleaseBlocked as exc:
            raise _error(exc) from exc

    @router.post("/{session_id}/{action}")
    async def action_request(session_id: UUID, action: Literal["authorize", "validate", "promote"],
                             body: ActionBody,
                             context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
                             session: AsyncSession = Depends(get_session)) -> dict[str, object]:  # noqa: B008
        require_account_write(context)
        try:
            return _data(await ReleaseSessions(context.exchange_account_id,
                context.deployment_environment).request_action(session, session_id,
                    action=action, operator=context.user_id, expected_revision=body.expected_revision,
                    now_ms=int(time.time() * 1000)))
        except ReleaseBlocked as exc:
            raise _error(exc) from exc

    return router
