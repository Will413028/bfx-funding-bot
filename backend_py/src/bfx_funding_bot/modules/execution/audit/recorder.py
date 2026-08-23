"""Fail-closed transaction boundary for pre-trade audit persistence."""
from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.execution.audit.model import ExecutionDecision
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow


class ExecutionAuditUnavailable(RuntimeError):  # noqa: N818 - public typed contract
    """Raised when durable audit persistence cannot be confirmed."""


class ExecutionDecisionRecorder:
    """Persist one decision before the execution path can reach a venue."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, decision: ExecutionDecision) -> None:
        """Commit one idempotent append-only insert or fail closed.

        `decision_id` is the idempotency key: concurrent/retried delivery of the
        same candidate can produce only one durable row. No conflicting update is
        attempted, so the original evidence remains immutable.
        """
        try:
            async with session_scope(self._session_factory) as session:
                await session.execute(
                    _idempotent_insert(session, decision.persistence_values())
                )
        except Exception as exc:
            raise ExecutionAuditUnavailable("execution decision audit is unavailable") from exc


def _idempotent_insert(session: AsyncSession, values: dict[str, object]) -> Any:
    bind = session.bind
    if bind is None:
        raise RuntimeError("audit session has no database bind")

    if bind.dialect.name == "postgresql":
        return postgresql_insert(ExecutionDecisionRow).values(**values).on_conflict_do_nothing(
            index_elements=("decision_id",)
        )
    if bind.dialect.name == "sqlite":
        return sqlite_insert(ExecutionDecisionRow).values(**values).on_conflict_do_nothing(
            index_elements=("decision_id",)
        )

    raise RuntimeError(f"unsupported audit database dialect: {bind.dialect.name}")
