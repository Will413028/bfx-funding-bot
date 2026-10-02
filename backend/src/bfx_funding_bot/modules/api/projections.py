"""Explicit ExchangeAccount projection read endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
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
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.ledger import Scope

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
        before: int | None = Query(default=None, description="event_seq cursor"),
        event_type: str | None = Query(default=None),
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        stmt = select(EventLogRow).where(
            EventLogRow.exchange_account_id == context.exchange_account_id,
            EventLogRow.deployment_environment == context.deployment_environment,
        )
        if before is not None:
            stmt = stmt.where(EventLogRow.event_seq < before)
        if event_type is not None:
            stmt = stmt.where(EventLogRow.event_type == event_type)
        rows = (
            await session.execute(
                stmt.order_by(EventLogRow.event_seq.desc()).limit(limit + 1)
            )
        ).scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        data = []
        for row in rows:
            payload = row.payload or {}
            amount = payload.get("amount") or payload.get("size_usdt")
            rate = payload.get("rate") if payload.get("rate") is not None else payload.get("fill_rate")
            data.append(
                ExecutionEventResponse(
                    event_seq=row.event_seq,
                    event_type=row.event_type,
                    occurred_at_ms=row.occurred_at_ms,
                    symbol=payload.get("symbol"),
                    venue_offer_id=row.venue_offer_id,
                    cid=row.cid,
                    amount=str(amount) if amount is not None else None,
                    rate=float(rate) if rate is not None else None,
                ).model_dump(by_alias=True)
            )
        return {
            "data": data,
            "pagination": {
                "hasMore": has_more,
                "nextBefore": rows[-1].event_seq if has_more and rows else None,
            },
        }

    return router
