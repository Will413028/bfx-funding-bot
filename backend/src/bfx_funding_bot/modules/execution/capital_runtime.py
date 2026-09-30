"""The live legacy capital authority's handles, for the legacy command boundary.

Reads go through the ledger facade's ports (``execution.legacy_ports`` builds
the legacy ones from this); the command gate still binds intents through the
repository here until its journal port lands (S1-3c2).
"""
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository


class CapitalRuntime:
    def __init__(self, *, repository: CapitalRepository,
                 session_factory: async_sessionmaker[AsyncSession], clock: Callable[[], int]) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.clock = clock
