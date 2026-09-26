"""Shared applied capital reader for planning, diagnostics and command guards."""
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.amount_fingerprint import fingerprints_in_use
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository, CapitalView


class CapitalRuntime:
    def __init__(self, *, repository: CapitalRepository,
                 session_factory: async_sessionmaker[AsyncSession], clock: Callable[[], int]) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.clock = clock

    async def read(self, *, symbol: str, cell_id: str,
                   session: AsyncSession | None = None) -> CapitalView:
        if session is not None:
            return await self.repository.read_capital(
                session, symbol=symbol, cell_id=cell_id, now_ms=self.clock(),
            )
        # Advisory-lock replay can update projections. A preview rolls those
        # updates back; only the command/snapshot owner may commit them.
        async with self.session_factory() as owned:
            return await self.read(symbol=symbol, cell_id=cell_id, session=owned)

    async def fingerprints_in_use(self, *, symbol: str, session: AsyncSession) -> frozenset[int]:
        """Amount fingerprints the planner must not reuse for ``symbol`` (D3a)."""
        return await fingerprints_in_use(session, account_id=self.repository.account_id,
                                         environment=self.repository.environment, symbol=symbol)
