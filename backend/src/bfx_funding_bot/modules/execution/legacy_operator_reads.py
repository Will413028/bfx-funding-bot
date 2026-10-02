"""Legacy ``OperatorReads``: the execution-uncertainty projection, as the API always read it."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.operator_evidence import ResolutionRejected
from bfx_funding_bot.modules.execution.uncertainty_resolution import (
    ResolutionScope,
    load_scoped_uncertainty,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.ledger import Scope, UncertaintyView


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
