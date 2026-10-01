"""Explicit ExchangeAccount projection read endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import ExchangeAccountContext, require_account_member
from bfx_funding_bot.modules.api.attribution import _dec_str
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import (
    ExecutionEventResponse,
    OfferClaimResponse,
    PositionResponse,
)
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)

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
    ) -> dict[str, object]:
        rows = (
            await session.execute(
                select(PositionStateRow)
                .where(
                    PositionStateRow.exchange_account_id == context.exchange_account_id,
                    PositionStateRow.deployment_environment == context.deployment_environment,
                )
                .order_by(PositionStateRow.symbol)
            )
        ).scalars().all()
        return {
            "data": [
                PositionResponse(
                    symbol=row.symbol,
                    reserved=_dec_str(row.reserved),
                    realized=_dec_str(row.realized),
                    n_credits=row.n_credits,
                    last_updated_ms=row.last_updated_ms,
                    last_reconciled_at=row.last_reconciled_at,
                ).model_dump(by_alias=True)
                for row in rows
            ]
        }

    @router.get("/exchange-accounts/{exchange_account_id}/offers")
    async def account_offers(
        state: str | None = None,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        stmt = select(OfferClaimRow).where(
            OfferClaimRow.exchange_account_id == context.exchange_account_id,
            OfferClaimRow.deployment_environment == context.deployment_environment,
        )
        if state is not None:
            stmt = stmt.where(OfferClaimRow.state == state)
        else:
            stmt = stmt.where(OfferClaimRow.state.in_(_ACTIVE_CLAIM_STATES))
        rows = (
            await session.execute(stmt.order_by(OfferClaimRow.last_updated_ms.desc()))
        ).scalars().all()
        return {
            "data": [
                OfferClaimResponse(
                    cid=row.cid,
                    venue_offer_id=row.venue_offer_id,
                    state=row.state,
                    symbol=row.symbol,
                    size_usdt=_dec_str(row.size_usdt),
                    occurred_at_ms=row.occurred_at_ms,
                    last_updated_ms=row.last_updated_ms,
                ).model_dump(by_alias=True)
                for row in rows
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
