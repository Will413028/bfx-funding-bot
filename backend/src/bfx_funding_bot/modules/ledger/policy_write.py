"""The one writer of the shared policy tables (``capital_policy_revisions`` / ``_heads``).

Both authorities append a policy revision here; they differ only in what they do before
(the legacy store replays its event stream under the scope lock). The caller owns the
transaction and holds the scope lock across the read it bases ``expected_revision`` on.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import PolicyRefused, Scope
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


async def write_policy_revision(
    session: AsyncSession, scope: Scope, *, symbol: str, policy: CapitalPolicy,
    expected_revision: int, source: dict[str, Any],
) -> AppliedPolicy:
    """Append revision ``expected_revision + 1`` and move the head, or raise ``PolicyRefused``.

    ``unsupported_enabled_symbol``: an unknown symbol, or fUSD enabled.
    ``revision_changed``: the head is not at ``expected_revision``.
    """
    if symbol not in POLICY_SYMBOLS or (symbol == "fUSD" and policy.enabled):
        raise PolicyRefused("unsupported_enabled_symbol")
    account = scope.exchange_account_id
    environment = scope.deployment_environment
    head = await session.get(
        CapitalPolicyHeadRow, (account, environment, symbol), populate_existing=True
    )
    version = head.revision if head is not None else 0
    if type(expected_revision) is not int or expected_revision != version:
        raise PolicyRefused("revision_changed")
    payload = policy_payload(policy)
    row = CapitalPolicyRevisionRow(
        id=uuid4(), exchange_account_id=account, deployment_environment=environment,
        symbol=symbol, revision=version + 1, schema_version=policy_schema_version(policy),
        policy=payload, digest=policy_digest(payload), source=source,
    )
    session.add(row)
    await session.flush()
    if head is None:
        session.add(CapitalPolicyHeadRow(
            exchange_account_id=account, deployment_environment=environment, symbol=symbol,
            revision_id=row.id, revision=row.revision,
        ))
    else:
        head.revision_id, head.revision = row.id, row.revision
    await session.flush()
    return AppliedPolicy(account, environment, symbol, row.revision, row.digest, row.id, policy)
