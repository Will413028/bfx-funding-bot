"""DB-backed 24h NAV window for ReconcileNavTracker (nav_window_samples, T9).

Like NavPeakStore: every call opens its own short session, and the tracker
swallows failures -- the reconcile money path never depends on this table.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.safety.tables import NavWindowSampleRow

WINDOW_MS = 24 * 60 * 60 * 1000
RETENTION_MS = 2 * WINDOW_MS


class NavWindowStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *,
                 account_id: UUID, deployment_environment: str) -> None:
        self._sf = session_factory
        self._account_id = account_id
        self._env = deployment_environment

    def _scope(self) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
        return (NavWindowSampleRow.exchange_account_id == self._account_id,
                NavWindowSampleRow.deployment_environment == self._env)

    async def load(self) -> dict[str, list[tuple[int, Decimal]]]:
        """Per symbol, the samples within 24h of that symbol's newest sample, oldest first."""
        async with self._sf() as session:
            newest = dict((await session.execute(
                select(NavWindowSampleRow.symbol, func.max(NavWindowSampleRow.occurred_at_ms))
                .where(*self._scope()).group_by(NavWindowSampleRow.symbol)
            )).tuples().all())
            samples: dict[str, list[tuple[int, Decimal]]] = defaultdict(list)
            for symbol, latest in newest.items():
                rows = (await session.execute(
                    select(NavWindowSampleRow.occurred_at_ms, NavWindowSampleRow.nav)
                    .where(*self._scope(), NavWindowSampleRow.symbol == symbol,
                           NavWindowSampleRow.occurred_at_ms >= latest - WINDOW_MS)
                    .order_by(NavWindowSampleRow.occurred_at_ms, NavWindowSampleRow.id)
                )).tuples().all()
                samples[symbol] = [(int(at), Decimal(str(nav))) for at, nav in rows]
        return dict(samples)

    async def save(self, symbol: str, occurred_at_ms: int, nav: Decimal) -> None:
        async with self._sf.begin() as session:
            session.add(NavWindowSampleRow(
                exchange_account_id=self._account_id, deployment_environment=self._env,
                symbol=symbol, occurred_at_ms=occurred_at_ms, nav=nav,
            ))
            await session.execute(delete(NavWindowSampleRow).where(
                *self._scope(), NavWindowSampleRow.symbol == symbol,
                NavWindowSampleRow.occurred_at_ms < occurred_at_ms - RETENTION_MS,
            ))
