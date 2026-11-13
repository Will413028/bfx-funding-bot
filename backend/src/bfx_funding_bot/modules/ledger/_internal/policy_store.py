"""The ledger's policy store: the policy heads and revisions, and nothing else.

The one reader and writer of ``capital_policy_revisions`` / ``_heads``. No event stream
is replayed or locked here. The caller holds the scope lock (``ScopeLock.lock``) across
a read and the write based on it, and owns the transaction.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import PolicyRefused
from bfx_funding_bot.modules.ledger.read.policy import LedgerPolicyReader
from bfx_funding_bot.modules.ledger.tables import CapitalPolicyHeadRow, CapitalPolicyRevisionRow
from bfx_funding_bot.modules.trading import (
    AppliedPolicy,
    CapitalPolicy,
    policy_digest,
    policy_payload,
    policy_schema_version,
)

# The symbols a policy may exist for; only the first may be enabled (the one funded currency).
POLICY_SYMBOLS = frozenset({"fUST", "fUSD"})


class LedgerPolicyStore(LedgerPolicyReader):
    async def apply_policy(
        self, session: AsyncSession, *, symbol: str, policy: CapitalPolicy,
        expected_revision: int, source: dict[str, Any],
        operator_request_id: UUID | None = None,
    ) -> AppliedPolicy:
        if symbol not in POLICY_SYMBOLS or (symbol == "fUSD" and policy.enabled):
            raise PolicyRefused("unsupported_enabled_symbol")
        account = self._scope.exchange_account_id
        head = await session.get(
            CapitalPolicyHeadRow, (account, symbol), populate_existing=True
        )
        version = head.revision if head is not None else 0
        if type(expected_revision) is not int or expected_revision != version:
            raise PolicyRefused("revision_changed")
        payload = policy_payload(policy)
        row = CapitalPolicyRevisionRow(
            id=uuid4(), exchange_account_id=account,
            symbol=symbol, revision=version + 1, schema_version=policy_schema_version(policy),
            policy=payload, digest=policy_digest(payload), source=source,
            operator_request_id=operator_request_id,
        )
        session.add(row)
        await session.flush()
        if head is None:
            session.add(CapitalPolicyHeadRow(
                exchange_account_id=account, symbol=symbol,
                revision_id=row.id, revision=row.revision,
            ))
        else:
            head.revision_id, head.revision = row.id, row.revision
        await session.flush()
        return AppliedPolicy(account, symbol, row.revision, row.digest, row.id,
                             policy)
