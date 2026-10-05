"""Explicit ExchangeAccount projection read endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import ExchangeAccountContext, require_account_member
from bfx_funding_bot.modules.api.attribution import _dec_str
from bfx_funding_bot.modules.api.deps import ReadModels, get_read_models, get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import (
    ExecutionEventResponse,
    OfferClaimResponse,
    PositionResponse,
)
from bfx_funding_bot.modules.ledger import ExecutionCursorError, Scope

_ACTIVE_CLAIM_STATES = ("pending", "unknown", "claimed")
_EXECUTIONS_LIMIT_CAP = 200


def build_projections_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1", tags=["projections"],
        dependencies=[Depends(shared_rate_limit_dependency())],
    )
    @router.get("/exchange-accounts/{exchange_account_id}/positions")
    async def account_positions(
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        views = await models.operator_reads.list_positions(
            session, Scope(context.exchange_account_id, context.deployment_environment)
        )
        return {
            "data": [
                PositionResponse(
                    symbol=view.symbol,
                    available=_dec_str(view.available),
                    offered=_dec_str(view.offered),
                    lent=_dec_str(view.lent),
                    unattributed_lent=(
                        _dec_str(view.unattributed_lent)
                        if view.unattributed_lent is not None
                        else None
                    ),
                    n_credits=view.n_credits,
                    last_updated_ms=view.last_updated_ms,
                    last_reconciled_at=view.last_reconciled_at_ms,
                ).model_dump(by_alias=True)
                for view in views
            ]
        }

    @router.get("/exchange-accounts/{exchange_account_id}/offers")
    async def account_offers(
        state: str | None = None,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        views = await models.operator_reads.list_offers(
            session,
            Scope(context.exchange_account_id, context.deployment_environment),
            states=_ACTIVE_CLAIM_STATES if state is None else (state,),
        )
        return {
            "data": [
                OfferClaimResponse(
                    offer_key=view.offer_key,
                    venue_offer_id=view.venue_offer_id,
                    state=view.state,
                    symbol=view.symbol,
                    size_usdt=_dec_str(view.size_usdt),
                    occurred_at_ms=view.occurred_at_ms,
                    last_updated_ms=view.last_updated_ms,
                ).model_dump(by_alias=True)
                for view in views
            ]
        }

    @router.get("/exchange-accounts/{exchange_account_id}/executions")
    async def account_executions(
        limit: int = Query(default=50, ge=1, le=_EXECUTIONS_LIMIT_CAP),
        before: str | None = Query(default=None, description="opaque cursor (nextBefore)"),
        event_type: str | None = Query(default=None),
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        try:
            page = await models.execution_history.list_executions(
                session,
                Scope(context.exchange_account_id, context.deployment_environment),
                before=before, limit=limit, event_type=event_type,
            )
        except ExecutionCursorError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_cursor"
            ) from None
        return {
            "data": [
                ExecutionEventResponse(
                    event_key=event.event_key,
                    event_type=event.event_type,
                    occurred_at_ms=event.occurred_at_ms,
                    symbol=event.symbol,
                    venue_offer_id=event.venue_offer_id,
                    cid=event.cid,
                    amount=event.amount,
                    rate=event.rate,
                ).model_dump(by_alias=True)
                for event in page.events
            ],
            "pagination": {
                "hasMore": page.next_before is not None,
                "nextBefore": page.next_before,
            },
        }

    return router
