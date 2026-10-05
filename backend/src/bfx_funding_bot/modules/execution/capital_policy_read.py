"""Read a scope's applied capital policy from the shared policy heads and revisions.

Both capital authorities keep these two tables (``ledger.tables``) and share one
writer (``ledger.policy_write``); this reader replays no event stream, so it
answers the same under either authority. ``CapitalBlockedError`` is the refusal
every capital read here and in the legacy repository raises.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger.tables import CapitalPolicyHeadRow, CapitalPolicyRevisionRow
from bfx_funding_bot.modules.trading import (
    CapitalPolicy,
    PolicyRejectedError,
    check_pointer,
    parse_policy,
)


class CapitalBlockedError(ValueError):
    """No authorization was issued; caller must not submit."""


def policy_from_row(row: CapitalPolicyRevisionRow) -> CapitalPolicy:
    """Validate one stored policy revision (schema, digest, exact keys) or refuse."""
    try:
        return parse_policy(row.schema_version, row.policy, row.digest)
    except PolicyRejectedError as exc:
        raise CapitalBlockedError(exc.reason) from exc


async def read_policy_row(session: AsyncSession, *, account_id: UUID, environment: str,
                          symbol: str) -> CapitalPolicyRevisionRow:
    """The revision the scope's policy head points at, or refuse."""
    head = await session.get(CapitalPolicyHeadRow, (account_id, environment, symbol),
                             populate_existing=True)
    row = None if head is None else await session.get(
        CapitalPolicyRevisionRow, head.revision_id, populate_existing=True)
    blocked = check_pointer(
        account_id, environment, symbol,
        None if head is None else (head.revision_id, head.revision),
        None if row is None else (row.id, row.exchange_account_id, row.deployment_environment,
                                  row.symbol, row.revision))
    if blocked is not None:
        raise CapitalBlockedError(blocked.reason)
    assert row is not None
    return row


async def read_policy_unlocked(session: AsyncSession, *, account_id: UUID, environment: str,
                               symbol: str) -> CapitalPolicy:
    """The applied policy without the account lock, for read-only pre-trade guards.

    The command boundary re-reads and binds the policy revision under the lock
    before any intent is written, so a guard reading a pointer that moves an
    instant later cannot authorise anything by itself.
    """
    row = await read_policy_row(session, account_id=account_id, environment=environment,
                                symbol=symbol)
    return policy_from_row(row)
