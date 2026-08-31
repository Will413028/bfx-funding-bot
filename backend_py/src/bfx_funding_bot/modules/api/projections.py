"""SP4 projection read endpoints — positions / offers / executions.

Read-only over position_state / offer_claims / event_log for the operator
console. Realm = BFX_ACCOUNT_ID / BFX_DEPLOYMENT_ENV env (v1: single bot
account, same convention as the attribution router; SP6 multi-tenant will
replace this with a user→account mapping). Every route behind require_operator.
INERT for the daemon: webapi only reads the shared tables.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_operator
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

log = logging.getLogger(__name__)

_ACTIVE_CLAIM_STATES = ("pending", "claimed")
_EXECUTIONS_LIMIT_CAP = 200


def build_projections_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1", tags=["projections"],
        dependencies=[Depends(shared_rate_limit_dependency())],
    )
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    deployment_environment = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")
    log.info(
        "projections_router realm account_id=%s deployment_environment=%s",
        account_id, deployment_environment,
    )

    @router.get("/positions")
    async def positions(
        user: Principal = Depends(require_operator),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        rows = (
            await session.execute(
                select(PositionStateRow)
                .where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == deployment_environment,
                )
                .order_by(PositionStateRow.symbol)
            )
        ).scalars().all()
        return {"data": [
            PositionResponse(
                symbol=r.symbol,
                reserved=_dec_str(r.reserved),
                realized=_dec_str(r.realized),
                n_credits=r.n_credits,
                last_updated_ms=r.last_updated_ms,
                last_reconciled_at=r.last_reconciled_at,
                last_event_seq=r.last_event_seq,
            ).model_dump(by_alias=True)
            for r in rows
        ]}

    @router.get("/offers")
    async def offers(
        state: str | None = None,
        user: Principal = Depends(require_operator),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        stmt = select(OfferClaimRow).where(
            OfferClaimRow.account_id == account_id,
            OfferClaimRow.deployment_environment == deployment_environment,
        )
        if state is not None:
            stmt = stmt.where(OfferClaimRow.state == state)
        else:
            stmt = stmt.where(OfferClaimRow.state.in_(_ACTIVE_CLAIM_STATES))
        stmt = stmt.order_by(OfferClaimRow.last_updated_ms.desc())
        rows = (await session.execute(stmt)).scalars().all()
        return {"data": [
            OfferClaimResponse(
                cid=r.cid,
                venue_offer_id=r.venue_offer_id,
                state=r.state,
                symbol=r.symbol,
                size_usdt=_dec_str(r.size_usdt),
                occurred_at_ms=r.occurred_at_ms,
                last_updated_ms=r.last_updated_ms,
            ).model_dump(by_alias=True)
            for r in rows
        ]}

    @router.get("/executions")
    async def executions(
        limit: int = Query(default=50, ge=1, le=_EXECUTIONS_LIMIT_CAP),
        before: int | None = Query(default=None, description="event_seq cursor"),
        event_type: str | None = Query(default=None),
        user: Principal = Depends(require_operator),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        stmt = select(EventLogRow).where(
            EventLogRow.account_id == account_id,
            EventLogRow.deployment_environment == deployment_environment,
        )
        if before is not None:
            stmt = stmt.where(EventLogRow.event_seq < before)
        if event_type is not None:
            stmt = stmt.where(EventLogRow.event_type == event_type)
        # Fetch one extra row purely as the has-more probe (contract v2:
        # pagination envelope replaces the FE "full page => more" heuristic).
        stmt = stmt.order_by(EventLogRow.event_seq.desc()).limit(limit + 1)
        rows = (await session.execute(stmt)).scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        out = []
        for r in rows:
            payload = r.payload or {}
            amount = payload.get("amount") or payload.get("size_usdt")
            rate = payload.get("rate") if payload.get("rate") is not None else payload.get("fill_rate")
            out.append(
                ExecutionEventResponse(
                    event_seq=r.event_seq,
                    event_type=r.event_type,
                    occurred_at_ms=r.occurred_at_ms,
                    symbol=payload.get("symbol"),
                    venue_offer_id=r.venue_offer_id,
                    cid=r.cid,
                    amount=str(amount) if amount is not None else None,
                    rate=float(rate) if rate is not None else None,
                ).model_dump(by_alias=True)
            )
        return {
            "data": out,
            "pagination": {
                "hasMore": has_more,
                "nextBefore": rows[-1].event_seq if has_more and rows else None,
            },
        }

    return router
