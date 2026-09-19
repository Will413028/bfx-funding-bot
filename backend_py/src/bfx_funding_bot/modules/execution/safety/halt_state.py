"""Persisted kill switch — DB-backed halt state for ManualKillGuard.

Before this, the canary halt existed only as BFX_KILL_SWITCH inside
canary.env. Nothing had to malfunction for real-money trading to resume: a
plain revert of that file, or a deploy from a branch without it, was enough,
and the resumption would have been silent. A halt is a decision about real
money and belongs in durable storage, not in whichever env file is on disk.

Append-only (see TradingHaltRow): every halt and resume is retained, so
"who resumed trading, when, and why" stays answerable.

Each call opens its own short session, like NavPeakStore — this is read on the
submit path and must not hold the daemon's long-lived sessions.

Failure posture is the opposite of NavPeakStore's: callers must treat an
unreadable halt state as HALTED. This module does not swallow exceptions; the
guard decides, and it fails closed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_uuid_or_none,
    account_scope_clause,
)
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow


@dataclass(frozen=True, slots=True)
class HaltState:
    halted: bool
    reason: str
    actor: str
    created_at_ms: int
    id: int


class HaltStateStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        account_id: str,
        deployment_environment: str,
    ) -> None:
        self._sf = session_factory
        self._account_id = account_id
        self._env = deployment_environment

    async def current(self, session: AsyncSession | None = None) -> HaltState | None:
        """Latest transition for this realm, or None if never configured.

        None is NOT "running normally" — it means no halt decision has ever
        been recorded here. Callers must keep the two distinguishable.
        """
        if session is None:
            async with self._sf() as owned:
                return await self.current(owned)
        else:
            row = (
                await session.execute(
                    select(TradingHaltRow)
                    .where(
                        account_scope_clause(
                            session,
                            account_id=self._account_id,
                            exchange_account_column=TradingHaltRow.exchange_account_id,
                            legacy_account_column=TradingHaltRow.account_id,
                        ),
                        TradingHaltRow.deployment_environment == self._env,
                    )
                    .order_by(TradingHaltRow.id.desc())
                    .limit(1)
                )
            ).scalars().first()
            return _to_state(row) if row is not None else None

    async def set_halted(
        self, halted: bool, *, reason: str, actor: str, now_ms: int | None = None,
    ) -> HaltState:
        """Append a transition. Never updates or deletes an existing row."""
        row = TradingHaltRow(
            account_id=self._account_id,
            exchange_account_id=account_id_uuid_or_none(self._account_id),
            deployment_environment=self._env,
            halted=halted,
            reason=reason,
            actor=actor,
            created_at_ms=now_ms if now_ms is not None else int(time.time() * 1000),
        )
        async with self._sf() as session:
            if account_id_uuid_or_none(self._account_id) is not None:
                await acquire_transaction_lock(
                    session, account_id=self._account_id, deployment_environment=self._env,
                )
            session.add(row)
            await session.commit()
            return _to_state(row)

    async def history(self, *, limit: int = 20) -> list[HaltState]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(TradingHaltRow)
                    .where(
                        account_scope_clause(
                            session,
                            account_id=self._account_id,
                            exchange_account_column=TradingHaltRow.exchange_account_id,
                            legacy_account_column=TradingHaltRow.account_id,
                        ),
                        TradingHaltRow.deployment_environment == self._env,
                    )
                    .order_by(TradingHaltRow.id.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [_to_state(r) for r in rows]


def _to_state(row: TradingHaltRow) -> HaltState:
    return HaltState(
        halted=row.halted, reason=row.reason, actor=row.actor,
        created_at_ms=row.created_at_ms, id=row.id,
    )
