"""Explicit operator amendment of one applied CapitalPolicy.

Changes ``enabled``, ``max_offer_amount`` and the offer envelope (lending
envelope ADR D1). Two callers: ``scripts/amend_capital_policy.py`` (owner
role, any field) and the daemon's ``CapitalPolicyRequestWorker`` (the
operator's UI enable/disable; the runtime role may change ``enabled`` only). Setting the envelope for the first time needs every envelope
field; afterwards any subset may change.

Same contract as the legacy conversion: a dry run returns a reviewable report
and its digest; apply recomputes the report in the same transaction, refuses if
anything moved (the policy revision, its digest or the requested value), and
appends exactly one new revision through the ``PolicyStore`` of the capital authority in force.
Nothing here writes a trading state, talks to the venue or resumes trading.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import PolicyRefused, PolicyStore, ScopeLock
from bfx_funding_bot.modules.trading import (
    CapitalPolicy,
    OfferEnvelope,
    policy_payload,
    policy_schema_version,
)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PolicyChanges:
    """Requested new values; None leaves a field as it is."""

    enabled: bool | None = None
    max_offer_amount: Decimal | None = None
    min_period_days: int | None = None
    max_period_days: int | None = None
    max_open_offers: int | None = None
    rate_floor_ratio: Decimal | None = None
    min_rate_apr: Decimal | None = None

    def as_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in self.__slots__
                if getattr(self, name) is not None}


_ENVELOPE_FIELDS = ("min_period_days", "max_period_days", "max_open_offers",
                    "rate_floor_ratio", "min_rate_apr")


def _amended(policy: CapitalPolicy, changes: PolicyChanges) -> CapitalPolicy:
    requested = {name: getattr(changes, name) for name in _ENVELOPE_FIELDS
                 if getattr(changes, name) is not None}
    envelope = policy.envelope
    if requested and envelope is None:
        missing = [name for name in _ENVELOPE_FIELDS if name not in requested]
        if missing:
            raise PolicyRefused(f"envelope_incomplete: {','.join(missing)}")
    try:
        if requested:
            envelope = (OfferEnvelope(**requested) if envelope is None
                        else replace(envelope, **requested))
        return replace(
            policy,
            enabled=policy.enabled if changes.enabled is None else changes.enabled,
            max_offer_amount=(policy.max_offer_amount if changes.max_offer_amount is None
                              else changes.max_offer_amount),
            envelope=envelope)
    except ValueError as exc:
        raise PolicyRefused(f"invalid_policy: {exc}") from exc


async def amend_capital_policy(
    session: AsyncSession, *, store: PolicyStore, scope_lock: ScopeLock,
    symbol: str, changes: PolicyChanges, apply_digest: str | None,
    origin: Mapping[str, str] | None = None, operator_request_id: UUID | None = None,
) -> dict[str, Any]:
    """Return the dry-run report, or apply exactly that report. The caller commits.

    ``origin`` is recorded in the new revision's ``source`` beside the digest
    (who asked, and why); it is not part of the report. ``operator_request_id``
    names the operator request the revision applies: the revision's typed
    column, and ``source.request_id`` as audit text. None for the owner's script.
    """
    if not changes.as_dict():
        raise PolicyRefused("no_changes_requested")
    await scope_lock.lock(session, store.scope)
    applied = await store.read_applied(session, symbol=symbol)
    amended = _amended(applied.policy, changes)
    report: dict[str, Any] = {
        "status": "dry_run", "account_id": str(store.scope.exchange_account_id),
        "environment": store.scope.deployment_environment, "symbol": symbol,
        "changes": changes.as_dict(),
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
        raise PolicyRefused("amendment_changed")
    written = await store.apply_policy(
        session, symbol=symbol, policy=amended, expected_revision=applied.revision,
        source={**(origin or {}),
                **({} if operator_request_id is None else {"request_id": str(operator_request_id)}),
                "amendment_digest": digest, "changes": changes.as_dict()},
        operator_request_id=operator_request_id,
    )
    report.update(status="applied", new_revision=written.revision,
                  new_revision_id=str(written.revision_id), new_policy_digest=written.digest)
    return report
