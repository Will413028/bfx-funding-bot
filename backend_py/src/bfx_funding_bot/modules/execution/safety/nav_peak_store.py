"""DB-backed _PeakStore for ReconcileNavTracker (nav_peak table).

Each call opens its own short session — save() runs inside the reconcile event
path, so it must not share/hold the daemon's long-lived sessions. Callers
(the tracker) swallow exceptions; this module just does the I/O.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.safety.tables import NavPeakRow


class NavPeakStore:
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

    async def load(self) -> dict[str, Decimal]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(NavPeakRow).where(
                        NavPeakRow.account_id == self._account_id,
                        NavPeakRow.deployment_environment == self._env,
                    )
                )
            ).scalars().all()
            return {r.symbol: Decimal(str(r.peak)) for r in rows}

    async def save(self, symbol: str, peak: Decimal, updated_at_ms: int) -> None:
        async with self._sf() as session:
            await session.merge(
                NavPeakRow(
                    account_id=self._account_id,
                    deployment_environment=self._env,
                    symbol=symbol,
                    peak=peak,
                    updated_at_ms=updated_at_ms,
                )
            )
            await session.commit()
