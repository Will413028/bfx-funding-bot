"""The applied capital policy of a scope, read only: the head, its revision, proven and parsed.

No snapshot transaction and no lock (policy rows only). The writer (``apply_policy``) is
``_internal.policy_store``; the capital read (``_internal.capital_reader``) reads the policy
through ``applied_policy`` in its own snapshot.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import PolicyRefused, Scope
from bfx_funding_bot.modules.ledger.tables import (
    CapitalPolicyHeadRow,
    CapitalPolicyRevisionRow,
)
from bfx_funding_bot.modules.trading import (
    AppliedPolicy,
    Blocked,
    PolicyRejectedError,
    check_pointer,
    parse_policy,
)


async def applied_policy(
    session: AsyncSession, account: UUID, environment: str, symbol: str
) -> AppliedPolicy | Blocked:
    """The symbol's head -> its revision, proven (``check_pointer``) and parsed."""
    head = (
        await session.execute(
            select(CapitalPolicyHeadRow.revision_id, CapitalPolicyHeadRow.revision).where(
                CapitalPolicyHeadRow.exchange_account_id == account,
                CapitalPolicyHeadRow.deployment_environment == environment,
                CapitalPolicyHeadRow.symbol == symbol,
            )
        )
    ).one_or_none()
    row: Row[Any] | None = None
    if head is not None:
        row = (await session.execute(
            select(
                CapitalPolicyRevisionRow.id,
                CapitalPolicyRevisionRow.exchange_account_id,
                CapitalPolicyRevisionRow.deployment_environment,
                CapitalPolicyRevisionRow.symbol,
                CapitalPolicyRevisionRow.revision,
                CapitalPolicyRevisionRow.schema_version,
                CapitalPolicyRevisionRow.policy,
                CapitalPolicyRevisionRow.digest,
            ).where(CapitalPolicyRevisionRow.id == head[0]).limit(1)
        )).first()
    blocked = check_pointer(
        account,
        environment,
        symbol,
        None if head is None else (head[0], head[1]),
        None
        if row is None
        else (
            row.id,
            row.exchange_account_id,
            row.deployment_environment,
            row.symbol,
            row.revision,
        ),
    )
    if blocked is not None:
        return blocked
    assert row is not None
    try:
        policy = parse_policy(row.schema_version, row.policy, row.digest)
    except PolicyRejectedError as exc:
        return Blocked(exc.reason, (("revision", str(row.id)),))
    return AppliedPolicy(account, environment, symbol, row.revision, row.digest, row.id, policy)


async def read_policy(session: AsyncSession, scope: Scope, symbol: str) -> AppliedPolicy | Blocked:
    """The symbol's applied policy; no snapshot transaction and no lock (policy rows only)."""
    return await applied_policy(session, scope.exchange_account_id, scope.deployment_environment, symbol)




class LedgerPolicyReader:
    def __init__(self, scope: Scope) -> None:
        self._scope = scope

    @property
    def scope(self) -> Scope:
        return self._scope

    async def read_applied(self, session: AsyncSession, *, symbol: str) -> AppliedPolicy:
        read = await read_policy(session, self._scope, symbol)
        if isinstance(read, Blocked):
            raise PolicyRefused(read.reason)
        return read


__all__ = [
    "LedgerPolicyReader",
    "applied_policy",
    "read_policy",
]
