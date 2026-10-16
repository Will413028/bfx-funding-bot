"""Operator commands: what an operator may ask for, checked once for every entry point.

The web API and the operator CLI are adapters over this module: it resolves the
account an operator acts on, validates a request (reason, symbol, evidence and its
allow-list, the operator named in it) and queues it in its outbox
(``execution.operator_requests``). Nothing here applies a request: the account
daemon's workers do, re-checking the operator under the account lock (ADR D4').

A refusal is an :class:`OperatorCommandError` with a bounded code; each adapter
maps its ``kind`` to its own surface (an HTTP status, an exit code). The checks run
in a fixed order that is part of the contract: account and membership before the
deployment environment, so a non-member learns nothing about the host; write role
before validation; the evidence reference before the operator named in the request.

The ledger's ports (``OperatorReads``, ``OperatorEvidence``, ``OperatorResolution``,
the policy store) are passed in: only ``apps`` may build them.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.settings import require_deployment_environment
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountNotFound,
    AccountRetired,
    AccountRole,
    MembershipDenied,
    get_exchange_account,
    require_account_membership,
)
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.operator_requests import insert_request
from bfx_funding_bot.modules.execution.safety.tables import (
    FundingCancelAllAuditRow,
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import to_state
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionRejected,
    ResolutionScope,
    UncertaintyResolutionRequests,
    request_rejection,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ResolutionAction,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    OperatorEvidence,
    OperatorReads,
    OperatorResolution,
    PolicyRefused,
    PolicyStore,
    ResolutionEvidence,
    ResolutionIntent,
    ResolutionSubject,
    Scope,
    UncertaintyView,
)
from bfx_funding_bot.modules.ledger import ResolutionRejected as EvidenceRejected
from bfx_funding_bot.modules.ledger.tables import CapitalPolicyHeadRow
from bfx_funding_bot.modules.trading import envelope_payload

RejectionKind = Literal["not_found", "forbidden", "conflict", "invalid", "unavailable"]
TradingAction = Literal["resume", "kill"]
CurrencyAction = Literal["enable", "disable"]

SYMBOL_PATTERN = r"^f[A-Z0-9]{2,15}$"
# Trading control and currency toggles: a reason is required and not blank.
CONTROL_REASON_MAX = 500
# Adjudications: the reason is optional except for a manual resolution.
RESOLUTION_REASON_MAX = 512
OPERATOR_ID_MAX = 256
VENUE_OFFER_ID_MAX = 256
MAX_EVIDENCE_BYTES = 16_384
# The evidence keys each adjudication accepts; anything else is refused, never stored.
RESOLUTION_EVIDENCE_KEYS: Mapping[ResolutionAction, frozenset[str]] = {
    "bind_to_venue": frozenset({"candidateCount", "candidate_count"}),
    "mark_not_accepted": frozenset({"candidateCount", "candidate_count"}),
    "manual_resolution": frozenset({"decision"}),
}
# Latest requests shown per currency, and for trading control.
CURRENCY_REQUESTS = 5
CONTROL_REQUESTS = 10


class OperatorCommandError(Exception):
    """A bounded, operator-readable reason a command is refused."""

    def __init__(self, code: str, *, kind: RejectionKind) -> None:
        super().__init__(code)
        self.code = code
        self.kind = kind


@dataclass(frozen=True, slots=True)
class OperatorContext:
    """The authorized account scope a command acts on."""

    exchange_account_id: UUID
    user_id: str
    role: AccountRole
    lifecycle_status: Literal["active", "halted"]
    deployment_environment: str

    @property
    def can_write(self) -> bool:
        return self.role in {"owner", "operator"}

    @property
    def scope(self) -> Scope:
        return Scope(self.exchange_account_id, self.deployment_environment)

    @property
    def resolution_scope(self) -> ResolutionScope:
        return ResolutionScope(self.exchange_account_id, self.deployment_environment)


def _not_found() -> OperatorCommandError:
    """One refusal for absent, retired, or unauthorized accounts and rows."""
    return OperatorCommandError("not_found", kind="not_found")


def _invalid(code: str) -> OperatorCommandError:
    return OperatorCommandError(code, kind="invalid")


def _rejected(exc: ResolutionRejected) -> OperatorCommandError:
    return OperatorCommandError(exc.code, kind=exc.kind)


async def resolve_context(
    session: AsyncSession, *, exchange_account_id: UUID, user_id: str,
) -> OperatorContext:
    """The account ``user_id`` may act on; absent, retired and foreign accounts look alike.

    The deployment environment is read last: a non-member is refused before
    anything about the host is.
    """
    try:
        account = await get_exchange_account(session, exchange_account_id=exchange_account_id)
        membership = await require_account_membership(
            session, exchange_account_id=exchange_account_id, user_id=user_id,
        )
    except (AccountNotFound, AccountRetired, MembershipDenied) as exc:
        raise _not_found() from exc
    # ``require_account_membership`` already excludes retired accounts. Keep the narrowing
    # explicit so a new lifecycle state cannot gain a command path without a decision.
    if account.lifecycle_status not in {"active", "halted"}:
        raise _not_found()
    try:
        environment = require_deployment_environment()
    except ValueError as exc:
        raise OperatorCommandError(
            "deployment_environment_not_configured", kind="unavailable") from exc
    return OperatorContext(
        exchange_account_id=exchange_account_id,
        user_id=user_id,
        role=membership.role,  # type: ignore[arg-type]  # DB check narrows this set
        lifecycle_status=account.lifecycle_status,  # type: ignore[arg-type]
        deployment_environment=environment,
    )


def require_write(context: OperatorContext) -> None:
    """Fail closed for a viewer's commands."""
    if not context.can_write:
        raise OperatorCommandError("account_write_required", kind="forbidden")


def control_reason(reason: str) -> str:
    """A trading-control or currency reason: required, not blank, bounded."""
    if not reason.strip() or len(reason) > CONTROL_REASON_MAX:
        raise _invalid("invalid_reason")
    return reason


# --- trading control and currencies -------------------------------------------------------

async def request_trading_control(
    session: AsyncSession, context: OperatorContext, *, action: TradingAction, reason: str,
    now_ms: int,
) -> UUID:
    """Queue a resume or kill; one pending request per action."""
    require_write(context)
    control_reason(reason)
    request_id = uuid4()
    if not await insert_request(session, TradingControlRequestRow, {
        "request_id": request_id, "exchange_account_id": context.exchange_account_id,
        "deployment_environment": context.deployment_environment, "action": action,
        "reason": reason, "requested_by": context.user_id, "created_at_ms": now_ms,
    }):
        raise OperatorCommandError("request_pending", kind="conflict")
    return request_id


async def request_currency(
    session: AsyncSession, context: OperatorContext, *, symbol: str, action: CurrencyAction,
    reason: str, now_ms: int,
) -> UUID:
    """Queue enabling or disabling one currency that has an applied policy."""
    require_write(context)
    if not re.fullmatch(SYMBOL_PATTERN, symbol):
        raise _invalid("invalid_symbol")
    control_reason(reason)
    # Only a currency with an applied policy can be toggled; the daemon re-reads it under
    # the account lock anyway.
    head = await session.get(CapitalPolicyHeadRow, (
        context.exchange_account_id, context.deployment_environment, symbol))
    if head is None:
        raise OperatorCommandError("policy_unavailable", kind="not_found")
    request_id = uuid4()
    if not await insert_request(session, CapitalPolicyRequestRow, {
        "request_id": request_id, "exchange_account_id": context.exchange_account_id,
        "deployment_environment": context.deployment_environment, "symbol": symbol,
        "action": action, "reason": reason, "requested_by": context.user_id,
        "created_at_ms": now_ms,
    }):
        raise OperatorCommandError("request_pending", kind="conflict")
    return request_id


def trading_request_view(row: TradingControlRequestRow) -> dict[str, Any]:
    return {
        "request_id": str(row.request_id), "action": row.action, "reason": row.reason,
        "requested_by": row.requested_by, "created_at_ms": row.created_at_ms,
        "state": row.state, "processed_at_ms": row.processed_at_ms,
        "outcome_reason": row.outcome_reason,
    }


def currency_request_view(row: CapitalPolicyRequestRow) -> dict[str, Any]:
    return {
        "request_id": str(row.request_id), "symbol": row.symbol, "action": row.action,
        "reason": row.reason, "requested_by": row.requested_by,
        "created_at_ms": row.created_at_ms, "state": row.state,
        "processed_at_ms": row.processed_at_ms, "outcome_reason": row.outcome_reason,
    }


async def get_trading_request(
    session: AsyncSession, context: OperatorContext, request_id: UUID,
) -> TradingControlRequestRow:
    row = await session.scalar(select(TradingControlRequestRow).where(
        TradingControlRequestRow.request_id == request_id,
        TradingControlRequestRow.exchange_account_id == context.exchange_account_id,
        TradingControlRequestRow.deployment_environment == context.deployment_environment,
    ).execution_options(populate_existing=True))
    if row is None:
        raise _not_found()
    return row


async def get_currency_request(
    session: AsyncSession, context: OperatorContext, request_id: UUID,
) -> CapitalPolicyRequestRow:
    row = await session.scalar(select(CapitalPolicyRequestRow).where(
        CapitalPolicyRequestRow.request_id == request_id,
        CapitalPolicyRequestRow.exchange_account_id == context.exchange_account_id,
        CapitalPolicyRequestRow.deployment_environment == context.deployment_environment,
    ).execution_options(populate_existing=True))
    if row is None:
        raise _not_found()
    return row


async def _currencies(session: AsyncSession, scope: Scope,
                      store: PolicyStore) -> list[dict[str, Any]]:
    """Every currency with an applied policy, as the daemon reads it.

    An unreadable policy is shown as such (the daemon refuses its offers too);
    it never hides the other currencies.
    """
    heads = (await session.scalars(select(CapitalPolicyHeadRow).where(
        CapitalPolicyHeadRow.exchange_account_id == scope.exchange_account_id,
        CapitalPolicyHeadRow.deployment_environment == scope.deployment_environment,
    ).order_by(CapitalPolicyHeadRow.symbol))).all()
    currencies = []
    for head in heads:
        entry: dict[str, Any] = {"symbol": head.symbol, "revision": head.revision,
                                 "policy_error": None, "enabled": None,
                                 "max_offer_amount": None, "envelope": None}
        # Unlocked: a view only (the command boundary re-reads and binds the revision under
        # the account lock before any intent).
        try:
            policy = (await store.read_applied(session, symbol=head.symbol)).policy
        except PolicyRefused as exc:
            entry["policy_error"] = str(exc)
        else:
            entry.update(
                enabled=policy.enabled,
                max_offer_amount=(None if policy.max_offer_amount is None
                                  else str(policy.max_offer_amount)),
                envelope=None if policy.envelope is None else envelope_payload(policy.envelope))
        requests = (await session.scalars(select(CapitalPolicyRequestRow).where(
            CapitalPolicyRequestRow.exchange_account_id == scope.exchange_account_id,
            CapitalPolicyRequestRow.deployment_environment == scope.deployment_environment,
            CapitalPolicyRequestRow.symbol == head.symbol,
        ).order_by(CapitalPolicyRequestRow.created_at_ms.desc(),
                   CapitalPolicyRequestRow.request_id.desc()).limit(CURRENCY_REQUESTS))).all()
        entry["requests"] = [currency_request_view(r) for r in requests]
        currencies.append(entry)
    return currencies


def _state(row: TradingStateRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    state = to_state(row)
    return {"id": state.id, "state": state.state, "cause": state.cause, "actor": state.actor,
            "reason": state.reason, "at_ms": state.created_at_ms}


async def _cancel_all(session: AsyncSession, row: TradingStateRow | None) -> list[dict[str, Any]]:
    """The venue cancel-all recorded for the state in force (only a HALTED has
    any): the latest phase per currency. A lone ``requested`` means the call's
    outcome was never recorded -- in flight, or interrupted."""
    if row is None:
        return []
    audits = (await session.scalars(select(FundingCancelAllAuditRow).where(
        FundingCancelAllAuditRow.trading_state_id == row.id,
    ).order_by(FundingCancelAllAuditRow.id))).all()
    latest: dict[str, FundingCancelAllAuditRow] = {}
    for audit in audits:
        latest[audit.currency] = audit
    return [{"currency": a.currency, "phase": a.phase, "detail": a.detail,
             "at_ms": a.occurred_at_ms} for a in sorted(latest.values(), key=lambda a: a.currency)]


async def trading_overview(
    session: AsyncSession, context: OperatorContext, store: PolicyStore,
) -> dict[str, Any]:
    """The trading state in force, its cancel-all, the latest requests and every currency."""
    scope = context.scope
    state = await session.scalar(select(TradingStateRow).where(
        TradingStateRow.exchange_account_id == scope.exchange_account_id,
        TradingStateRow.deployment_environment == scope.deployment_environment,
    ).order_by(TradingStateRow.id.desc()).limit(1))
    requests = (await session.scalars(select(TradingControlRequestRow).where(
        TradingControlRequestRow.exchange_account_id == scope.exchange_account_id,
        TradingControlRequestRow.deployment_environment == scope.deployment_environment,
    ).order_by(TradingControlRequestRow.created_at_ms.desc(),
               TradingControlRequestRow.request_id.desc()).limit(CONTROL_REQUESTS))).all()
    return {
        "trading_state": _state(state),
        "cancel_all": await _cancel_all(session, state),
        "requests": [trading_request_view(r) for r in requests],
        "currencies": await _currencies(session, scope, store),
    }


# --- uncertainty adjudication -------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResolutionCommand:
    """One adjudication as the operator stated it, before validation."""

    uncertainty_id: UUID
    action: ResolutionAction
    evidence_ref: str
    operator_uuid: str | None = None
    reason: str | None = None
    evidence: Mapping[str, Any] | None = None
    venue_offer_id: str | None = None


def bounded_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach and cap evidence before it becomes an immutable audit value."""
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True)
        if len(encoded.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence too large")
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _invalid("invalid_resolution_evidence") from exc
    if not isinstance(decoded, dict):  # defensive: JSON object was required
        raise _invalid("invalid_resolution_evidence")
    return decoded


def resolution_evidence(action: ResolutionAction, supplied: Mapping[str, Any]) -> dict[str, Any]:
    """The bounded evidence, refused when it names a key outside the action's allow-list."""
    evidence = bounded_evidence(supplied)
    if not set(evidence).issubset(RESOLUTION_EVIDENCE_KEYS[action]):
        raise _invalid("invalid_resolution_evidence")
    return evidence


def manual_decision(evidence: Mapping[str, Any]) -> str | None:
    """The decision string, or None; the allow-list check happens after kind.

    Keeping the value check in the shared validator preserves the code an
    operator sees when both the kind and the decision are wrong.
    """
    decision = evidence.get("decision")
    if set(evidence) != {"decision"} or not isinstance(decision, str):
        return None
    return decision


def _operator_id(context: OperatorContext, supplied: str | None) -> str:
    value = (supplied or context.user_id).strip()
    if not value:
        raise _invalid("operator_required")
    if value != context.user_id:
        # Do not allow an operator to write an audit event under another immutable
        # principal's identity, even if the request is otherwise account-authorized.
        raise OperatorCommandError("operator_identity_mismatch", kind="forbidden")
    return value


def _check_lengths(command: ResolutionCommand) -> None:
    """The bounds the web API's request model also enforces, for every other adapter."""
    if command.reason is not None and len(command.reason) > RESOLUTION_REASON_MAX:
        raise _invalid("invalid_reason")
    if command.action == "manual_resolution" and not command.reason:
        raise _invalid("invalid_reason")
    if command.operator_uuid is not None and not 1 <= len(command.operator_uuid) <= OPERATOR_ID_MAX:
        raise _invalid("operator_required")
    if command.action == "bind_to_venue" and not (
            command.venue_offer_id and len(command.venue_offer_id) <= VENUE_OFFER_ID_MAX):
        raise _invalid("invalid_venue_offer_id")


async def request_resolution(
    session: AsyncSession, context: OperatorContext, resolution: OperatorResolution,
    command: ResolutionCommand, *, now_ms: int,
) -> UncertaintyResolutionRequestRow:
    """Validate an adjudication and queue it (an identical repeat is the waiting request)."""
    require_write(context)
    _check_lengths(command)
    evidence = resolution_evidence(command.action, command.evidence or {})
    try:
        # The reference is judged before who asked, as it always was.
        resolution.columns(command.evidence_ref)
    except EvidenceRejected as exc:
        raise _rejected(request_rejection(exc)) from exc
    intent = ResolutionIntent(
        uncertainty_id=command.uncertainty_id,
        action=command.action,
        evidence_ref=command.evidence_ref,
        operator_id=_operator_id(context, command.operator_uuid),
        reason=command.reason,
        venue_offer_id=command.venue_offer_id if command.action == "bind_to_venue" else None,
        decision=manual_decision(evidence) if command.action == "manual_resolution" else None,
    )
    try:
        return await UncertaintyResolutionRequests(context.resolution_scope, resolution).request(
            session, intent, now_ms=now_ms)
    except ResolutionRejected as exc:
        raise _rejected(exc) from exc


async def get_resolution_request(
    session: AsyncSession, context: OperatorContext, request_id: UUID,
) -> UncertaintyResolutionRequestRow:
    try:
        return await UncertaintyResolutionRequests(context.resolution_scope).get(
            session, request_id)
    except ResolutionRejected as exc:
        raise _rejected(exc) from exc


@dataclass(frozen=True, slots=True)
class UncertaintyDetail:
    """One uncertainty, the evidence an adjudication would cite, and its newest request."""

    view: UncertaintyView
    context: ResolutionEvidence
    latest_request: UncertaintyResolutionRequestRow | None


def _unavailable() -> OperatorCommandError:
    return OperatorCommandError("uncertainties_unavailable", kind="unavailable")


async def _details(
    session: AsyncSession, context: OperatorContext, evidence: OperatorEvidence,
    rows: Sequence[UncertaintyView],
) -> list[UncertaintyDetail]:
    latest = await UncertaintyResolutionRequests(context.resolution_scope).latest_for(
        session, [row.uncertainty_id for row in rows])
    return [
        UncertaintyDetail(
            view=row,
            context=await evidence.resolution_context(
                session, context.scope,
                ResolutionSubject(row.uncertainty_id, row.symbol, row.attempt_id)),
            latest_request=latest.get(row.uncertainty_id),
        )
        for row in rows
    ]


async def list_uncertainties(
    session: AsyncSession, context: OperatorContext, reads: OperatorReads,
    evidence: OperatorEvidence, *, state: Literal["open", "resolved"] | None, limit: int,
) -> list[UncertaintyDetail]:
    """The account's uncertainties; any failure to read them is one bounded refusal."""
    try:
        rows = await reads.list_uncertainties(session, context.scope, state=state, limit=limit)
        return await _details(session, context, evidence, rows)
    except Exception as exc:
        raise _unavailable() from exc


async def get_uncertainty(
    session: AsyncSession, context: OperatorContext, reads: OperatorReads,
    evidence: OperatorEvidence, uncertainty_id: UUID,
) -> UncertaintyDetail:
    row = await reads.get_uncertainty(session, context.scope, uncertainty_id)
    if row is None:
        raise _not_found()
    try:
        (detail,) = await _details(session, context, evidence, [row])
    except Exception as exc:
        raise _unavailable() from exc
    return detail


def evidence_summary(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose only allowlisted, bounded evidence fields.

    In particular, arbitrary response payloads, credentials, signed headers,
    and request bodies never cross this boundary. ``coverage`` is kept as a
    small boolean map because it is useful to an operator without exposing
    the venue response itself.
    """
    if not isinstance(value, Mapping):
        return {}
    aliases = {
        "reason": "outcomeReason",
        "outcome_reason": "outcomeReason",
        "observed_at_ms": "observedAtMs",
        "candidate_count": "candidateCount",
        "candidateCount": "candidateCount",
        "venue_offer_id": "venueOfferId",
        "venueOfferId": "venueOfferId",
        "status": "status",
        "coverage": "coverage",
    }
    result: dict[str, Any] = {}
    for key, output_key in aliases.items():
        if key not in value or output_key in result:
            continue
        item = value[key]
        if key == "coverage" and isinstance(item, Mapping):
            result[output_key] = {
                str(name): bool(flag)
                for name, flag in list(item.items())[:12]
                if isinstance(flag, bool)
            }
            continue
        scalar = _safe_scalar(item)
        if scalar is not None or item is None:
            result[output_key] = scalar
        if len(result) >= 12:
            break
    return result


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            return value[:256]
        return value
    return None


__all__ = [
    "CONTROL_REASON_MAX",
    "RESOLUTION_EVIDENCE_KEYS",
    "RESOLUTION_REASON_MAX",
    "SYMBOL_PATTERN",
    "CurrencyAction",
    "OperatorCommandError",
    "OperatorContext",
    "ResolutionCommand",
    "TradingAction",
    "UncertaintyDetail",
    "bounded_evidence",
    "control_reason",
    "currency_request_view",
    "evidence_summary",
    "get_currency_request",
    "get_resolution_request",
    "get_trading_request",
    "get_uncertainty",
    "list_uncertainties",
    "manual_decision",
    "request_currency",
    "request_resolution",
    "request_trading_control",
    "require_write",
    "resolution_evidence",
    "resolve_context",
    "trading_overview",
    "trading_request_view",
]
