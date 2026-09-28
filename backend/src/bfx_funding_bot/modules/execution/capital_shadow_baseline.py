"""Observe the legacy capital fold without its authorizing prepare path."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.capital_repository import (
    AppliedCapitalPolicy,
    CapitalBlockedError,
    CapitalRepository,
    policy_from_row,
)
from bfx_funding_bot.modules.execution.capital_shadow_port import (
    BaselineAvailable,
    BaselineBlocked,
    BaselineNotComparable,
    BaselineResult,
    CapitalScopeLike,
)
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalPolicyHeadRow,
    CapitalPolicyRevisionRow,
    CapitalSnapshotRow,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, ProjectionHeadRow


async def read_baseline(
    session: AsyncSession, *, scope: CapitalScopeLike, now_ms: int, max_snapshot_age_ms: int
) -> BaselineResult:
    """Caller supplies a clean, active REPEATABLE READ READ ONLY transaction."""
    if not session.in_transaction() or session.new or session.dirty or session.deleted:
        raise ValueError("caller_owned_clean_transaction_required")
    repo = CapitalRepository(
        account_id=scope.account_id,
        environment=scope.environment,
        max_snapshot_age_ms=max_snapshot_age_ms,
    )
    with session.no_autoflush:
        watermark = int(await session.scalar(select(func.max(EventLogRow.event_seq)).where(
            EventLogRow.exchange_account_id == scope.account_id,
            EventLogRow.deployment_environment == scope.environment,
        )) or 0)
        head = await session.get(
            ProjectionHeadRow, (scope.account_id, scope.environment, "execution_state"),
            populate_existing=True,
        )
        cursor = head.last_event_seq if head is not None else None
        if cursor != watermark:
            return BaselineNotComparable(
                "projection_cursor_lag" if cursor is not None and cursor < watermark
                else "projection_integrity", cursor, watermark,
            )
        try:
            policy_head = await session.get(
                CapitalPolicyHeadRow, (scope.account_id, scope.environment, scope.symbol),
                populate_existing=True,
            )
            if policy_head is None:
                raise CapitalBlockedError("policy_unavailable")
            row = await session.get(
                CapitalPolicyRevisionRow, policy_head.revision_id, populate_existing=True,
            )
            if row is None or (
                row.exchange_account_id, row.deployment_environment, row.symbol, row.revision
            ) != (
                scope.account_id, scope.environment, scope.symbol, policy_head.revision
            ):
                raise CapitalBlockedError("inconsistent_policy_pointer")
            applied = AppliedCapitalPolicy(row.revision, row.digest, policy_from_row(row), row.id)
            view = await repo._read_capital(
                session, symbol=scope.symbol, cell_id=scope.cell_id, now_ms=now_ms,
                applied=applied,
            )
            accepted = await session.get(CapitalSnapshotRow, view.snapshot_seq)
            if accepted is None or (
                accepted.exchange_account_id, accepted.deployment_environment
            ) != (scope.account_id, scope.environment):
                raise CapitalBlockedError("snapshot_evidence_conflict")
        except CapitalBlockedError as exc:
            return BaselineBlocked(str(exc), cursor, watermark)
        return BaselineAvailable(
            scope.account_id, scope.environment, scope.symbol,
            applied.revision, applied.digest, applied.revision_id, applied.policy,
            view.snapshot_seq, accepted.command_fence, accepted.query_id,
            view.snapshot, view.budget, view.unattributed_credit_exposure,
            cursor, watermark,
        )
