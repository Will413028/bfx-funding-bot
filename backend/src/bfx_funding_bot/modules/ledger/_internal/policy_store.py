"""The ledger's policy store: the shared policy heads and revisions, and nothing else.

No event stream is replayed or locked here. The caller holds the scope lock
(``ScopeLock.lock``) across a read and the write based on it; the write itself is the
one writer both authorities share (``ledger.policy_write``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import PolicyRefused, Scope
from bfx_funding_bot.modules.ledger._internal import capital_reader
from bfx_funding_bot.modules.ledger.policy_write import write_policy_revision
from bfx_funding_bot.modules.trading import AppliedPolicy, Blocked, CapitalPolicy


class LedgerPolicyStore:
    def __init__(self, scope: Scope) -> None:
        self._scope = scope

    @property
    def scope(self) -> Scope:
        return self._scope

    async def read_applied(self, session: AsyncSession, *, symbol: str) -> AppliedPolicy:
        read = await capital_reader.read_policy(session, self._scope, symbol)
        if isinstance(read, Blocked):
            raise PolicyRefused(read.reason)
        return read

    async def apply_policy(
        self, session: AsyncSession, *, symbol: str, policy: CapitalPolicy,
        expected_revision: int, source: dict[str, Any],
        operator_request_id: UUID | None = None,
    ) -> AppliedPolicy:
        return await write_policy_revision(
            session, self._scope, symbol=symbol, policy=policy,
            expected_revision=expected_revision, source=source,
            operator_request_id=operator_request_id,
        )
