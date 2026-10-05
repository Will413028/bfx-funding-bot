"""Legacy ``OperatorReads`` and ``ExecutionHistory``: the execution-uncertainty projection and
the event log, as the API always read them."""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.operator_evidence import ResolutionRejected
from bfx_funding_bot.modules.execution.uncertainty_requests import ResolutionScope
from bfx_funding_bot.modules.execution.uncertainty_resolution import load_scoped_uncertainty
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.ledger import (
    ExecutionCursorError,
    ExecutionEventView,
    ExecutionPage,
    OfferView,
    PositionView,
    Scope,
    UncertaintyView,
)


def _view(row: ExecutionUncertaintyRow) -> UncertaintyView:
    return UncertaintyView(
        uncertainty_id=row.uncertainty_id,
        kind=row.kind,
        symbol=row.symbol,
        state=row.state,
        intended_amount=row.intended_amount,
        evidence=row.evidence,
        attempt_id=row.attempt_id,
        opened_at_ms=int(row.opened_at.timestamp() * 1000),
        resolved_at_ms=(
            int(row.resolved_at.timestamp() * 1000) if row.resolved_at is not None else None
        ),
        resolved_by_operator_id=row.resolved_by_operator_id,
        resolution_reason=row.resolution_reason,
    )


class LegacyOperatorReads:
    async def list_uncertainties(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        state: Literal["open", "resolved"] | None,
        limit: int,
    ) -> tuple[UncertaintyView, ...]:
        stmt = (
            select(ExecutionUncertaintyRow)
            .where(
                ExecutionUncertaintyRow.exchange_account_id == scope.exchange_account_id,
                ExecutionUncertaintyRow.deployment_environment == scope.deployment_environment,
            )
            .order_by(ExecutionUncertaintyRow.opened_event_seq.desc())
            .limit(limit)
        )
        if state is not None:
            stmt = stmt.where(ExecutionUncertaintyRow.state == state)
        return tuple(_view(row) for row in (await session.execute(stmt)).scalars().all())

    async def get_uncertainty(
        self, session: AsyncSession, scope: Scope, uncertainty_id: UUID
    ) -> UncertaintyView | None:
        try:
            row = await load_scoped_uncertainty(
                session,
                ResolutionScope(scope.exchange_account_id, scope.deployment_environment),
                uncertainty_id,
            )
        except ResolutionRejected:
            return None
        return _view(row)

    async def list_positions(
        self, session: AsyncSession, scope: Scope
    ) -> tuple[PositionView, ...]:
        rows = (
            await session.execute(
                select(PositionStateRow)
                .where(
                    PositionStateRow.exchange_account_id == scope.exchange_account_id,
                    PositionStateRow.deployment_environment == scope.deployment_environment,
                )
                .order_by(PositionStateRow.symbol)
            )
        ).scalars().all()
        return tuple(
            PositionView(
                symbol=row.symbol,
                available=row.available_amount,
                offered=row.offered_amount,
                lent=row.lent_amount,
                unattributed_lent=None,  # the legacy projection has no such fact
                n_credits=row.n_credits,
                last_updated_ms=row.last_updated_ms,
                last_reconciled_at_ms=row.last_reconciled_at,
            )
            for row in rows
        )

    async def list_offers(
        self, session: AsyncSession, scope: Scope, *, states: Collection[str]
    ) -> tuple[OfferView, ...]:
        rows = (
            await session.execute(
                select(OfferClaimRow)
                .where(
                    OfferClaimRow.exchange_account_id == scope.exchange_account_id,
                    OfferClaimRow.deployment_environment == scope.deployment_environment,
                    OfferClaimRow.state.in_(sorted(states)),
                )
                .order_by(OfferClaimRow.last_updated_ms.desc())
            )
        ).scalars().all()
        return tuple(
            OfferView(
                offer_key=str(row.cid),
                venue_offer_id=row.venue_offer_id,
                state=row.state,
                symbol=row.symbol,
                size_usdt=row.size_usdt,
                occurred_at_ms=row.occurred_at_ms,
                last_updated_ms=row.last_updated_ms,
            )
            for row in rows
        )


class LegacyExecutionHistory:
    """The scope's ``event_log``, newest first; the cursor is the last row's ``event_seq``.

    Also the archive the ledger's history continues into after the switch (the event log is
    frozen then, ``e8f9a0b1c2d3``).
    """

    async def list_executions(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        before: str | None,
        limit: int,
        event_type: str | None,
    ) -> ExecutionPage:
        stmt = select(EventLogRow).where(
            EventLogRow.exchange_account_id == scope.exchange_account_id,
            EventLogRow.deployment_environment == scope.deployment_environment,
        )
        if before is not None:
            if not before.isascii() or not before.isdigit():
                raise ExecutionCursorError(before)
            stmt = stmt.where(EventLogRow.event_seq < int(before))
        if event_type is not None:
            stmt = stmt.where(EventLogRow.event_type == event_type)
        rows = (
            await session.execute(stmt.order_by(EventLogRow.event_seq.desc()).limit(limit + 1))
        ).scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = []
        for row in rows:
            payload = row.payload or {}
            amount = payload.get("amount") or payload.get("size_usdt")
            rate = payload.get("rate") if payload.get("rate") is not None else payload.get("fill_rate")
            events.append(ExecutionEventView(
                event_key=str(row.event_seq),
                event_type=row.event_type,
                occurred_at_ms=row.occurred_at_ms,
                symbol=payload.get("symbol"),
                venue_offer_id=row.venue_offer_id,
                cid=row.cid,
                amount=str(amount) if amount is not None else None,
                rate=float(rate) if rate is not None else None,
            ))
        return ExecutionPage(
            tuple(events), str(rows[-1].event_seq) if has_more and rows else None
        )
