"""Operator enable/disable of one currency, applied by the account daemon.

Lending envelope ADR 2026-09-25 D4: the per-currency CapitalPolicy ``enabled``
flag is the everyday stop. The web API only inserts an ``enable`` or
``disable`` request (``capital_policy_requests``, the operator-request outbox);
the :class:`CapitalPolicyRequestWorker` applies it under the account lock after
re-checking the operator, through the amendment path
(``accounts.capital_amendment`` → ``CapitalRepository.apply_policy``), and
records the revision in force on the row.

What follows a disable is not done here: ``DeploymentReconciler._pull_if_stopped``
reads the applied policy every tick, plans nothing for a disabled currency and
cancels its managed offers. Nothing here checks the running build, the trading
state or the envelope: disabling depends on nothing but the operator, and
enabling a currency without an envelope is allowed (the offer-envelope guard
refuses its offers until one is set).
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.capital_amendment import (
    PolicyChanges,
    amend_capital_policy,
)
from bfx_funding_bot.modules.execution.capital_repository import (
    CapitalBlockedError,
    CapitalRepository,
)
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.operator_requests import (
    APPLIED,
    REJECTED,
    OperatorRequestWorker,
    Outcome,
    RejectionKind,
    RequestRejected,
)
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

CAPITAL_POLICY_REQUEST_APPLIED = "capital_policy_request_applied"
CAPITAL_POLICY_REQUEST_REJECTED = "capital_policy_request_rejected"
CAPITAL_POLICY_REQUEST_FAILED = "capital_policy_request_failed"

_REJECTION_KINDS: dict[str, RejectionKind] = {
    "policy_unavailable": "not_found",
    "unsupported_enabled_symbol": "invalid",
}


class CapitalPolicyRequestWorker(OperatorRequestWorker[CapitalPolicyRequestRow, None]):
    """Applies enable/disable requests, oldest first, one policy revision each.

    Asking for the state already in force is applied without a new revision;
    the row names the revision that was already in force.
    """

    model = CapitalPolicyRequestRow
    name = "capital_policy_request"

    def _repository(self) -> CapitalRepository:
        # Only the policy is read and written here; the snapshot age is unused.
        return CapitalRepository(account_id=self.account_id, environment=self.environment,
                                 max_snapshot_age_ms=60_000)

    async def apply(self, session: AsyncSession, row: CapitalPolicyRequestRow,
                    prepared: None) -> Outcome:
        repository = self._repository()
        enabled = row.action == "enable"
        changes = PolicyChanges(enabled=enabled)
        try:
            report = await amend_capital_policy(session, repository=repository, symbol=row.symbol,
                                                changes=changes, apply_digest=None)
            if report["status"] == "unchanged":
                current = await repository.read_applied(session, symbol=row.symbol)
                return Outcome(APPLIED, f"unchanged: already {'enabled' if enabled else 'disabled'}",
                               columns={"policy_revision_id": current.revision_id})
            written = await amend_capital_policy(
                session, repository=repository, symbol=row.symbol, changes=changes,
                apply_digest=report["amendment_digest"],
                origin={"request_id": str(row.request_id), "requested_by": row.requested_by,
                        "reason": row.reason})
        except CapitalBlockedError as exc:
            code = str(exc)
            raise RequestRejected(code, kind=_REJECTION_KINDS.get(code, "conflict")) from exc
        note = "enabled" if enabled else "disabled"
        if enabled and "envelope" not in written["new_policy"]:
            note = "enabled; envelope unset: offers are refused until it is set"
        return Outcome(APPLIED, f"{note} (revision {written['new_revision']})",
                       columns={"policy_revision_id": UUID(written["new_revision_id"])})

    async def committed(self, row: CapitalPolicyRequestRow, outcome: Outcome) -> None:
        fields: dict[str, Any] = {"request_id": str(row.request_id), "symbol": row.symbol,
                                  "action": row.action, "by": row.requested_by}
        if outcome.state == APPLIED:
            alerts.emit(CAPITAL_POLICY_REQUEST_APPLIED, level=alerts.WARNING,
                        outcome=outcome.reason, **fields)
        elif outcome.state == REJECTED:
            alerts.emit(CAPITAL_POLICY_REQUEST_REJECTED, level=alerts.WARNING,
                        reason=outcome.reason, **fields)
        else:
            alerts.emit(CAPITAL_POLICY_REQUEST_FAILED, level=alerts.CRITICAL,
                        reason=outcome.reason, **fields)


__all__ = [
    "CAPITAL_POLICY_REQUEST_APPLIED",
    "CAPITAL_POLICY_REQUEST_FAILED",
    "CAPITAL_POLICY_REQUEST_REJECTED",
    "CapitalPolicyRequestWorker",
]
