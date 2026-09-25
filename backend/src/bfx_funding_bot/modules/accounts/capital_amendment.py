"""Explicit operator amendment of one applied CapitalPolicy (T9: max_offer_amount).

Same contract as the legacy conversion: a dry run returns a reviewable report
and its digest; apply recomputes the report in the same transaction, refuses if
anything moved (the policy revision, its digest or the requested value), and
appends exactly one new revision through ``CapitalRepository.apply_policy``.
Nothing here writes a trading state, talks to the venue or resumes trading.
"""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.capital_repository import (
    CapitalBlockedError,
    CapitalRepository,
    policy_payload,
    policy_schema_version,
)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def amend_capital_policy(
    session: AsyncSession, *, repository: CapitalRepository, symbol: str,
    max_offer_amount: Decimal, apply_digest: str | None,
) -> dict[str, Any]:
    """Return the dry-run report, or apply exactly that report. The caller commits."""
    if (not isinstance(max_offer_amount, Decimal) or not max_offer_amount.is_finite()
            or max_offer_amount <= 0):
        raise CapitalBlockedError("invalid_max_offer_amount")
    await repository.writer.prepare_locked(session, account_id=repository.account_id)
    applied = await repository.read_applied(session, symbol=symbol)
    amended = replace(applied.policy, max_offer_amount=max_offer_amount)
    report: dict[str, Any] = {
        "status": "dry_run", "account_id": str(repository.account_id),
        "environment": repository.environment, "symbol": symbol,
        "expected_revision": applied.revision, "current_policy_digest": applied.digest,
        "current_policy": policy_payload(applied.policy),
        "new_policy": policy_payload(amended),
        "new_schema_version": policy_schema_version(amended), "resumed": False,
    }
    if amended == applied.policy:
        report["status"] = "unchanged"
        return report
    digest = _digest(report)
    report["amendment_digest"] = digest
    if apply_digest is None:
        return report
    if apply_digest != digest:
        raise CapitalBlockedError("amendment_changed")
    written = await repository.apply_policy(
        session, symbol=symbol, policy=amended, expected_revision=applied.revision,
        source={"amendment_digest": digest,
                "changes": {"max_offer_amount": str(max_offer_amount)}},
    )
    report.update(status="applied", new_revision=written.revision,
                  new_policy_digest=written.digest)
    return report
