"""Account-scoped operator endpoints for execution uncertainties.

The rows returned by this module are deliberately small, bounded read DTOs.
Resolution is an append-only domain event: the handler validates the account
scope and reconcile fence, then delegates to :class:`AccountEventWriter`.
It must not call ``UncertaintyService.resolve`` because that legacy helper
updates a projection directly and would make a clean event-log replay diverge.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    require_account_member,
    require_account_write,
)
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.event_store.writer import (
    AccountEventWriter,
    ProjectionWriteError,
)
from bfx_funding_bot.modules.execution.events import (
    MANUAL_UNCERTAINTY_RESOLUTION_ACTIONS,
    ManualUncertaintyResolutionAction,
    UncertaintyBoundToVenueOffer,
    UncertaintyManuallyResolved,
    UncertaintyMarkedNotAccepted,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.execution.unknown_matching import (
    attempt_from_row,
    deterministic_resolution_evidence,
    match_attempt_to_snapshot,
)

_MAX_LIMIT = 100
_MAX_REASON_LENGTH = 512
_MAX_EVIDENCE_BYTES = 16_384
_RECONCILE_EVENT_TYPE = "VENUE_SNAPSHOT_OBSERVED"
_SUPPORTED_KINDS = frozenset(
    {
        "submit_outcome_unknown",
        "unattributed_venue_offer",
        "unsupported_venue_exposure",
    }
)
_EvidenceValue = str | int | float | bool | None | dict[str, Any] | list[Any]


class UncertaintyResolutionRequest(BaseModel):
    """Common fresh-reconcile fence and optional operator identity."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    reconcile_event_seq: int = Field(
        alias="reconcileEventSeq",
        ge=0,
    )
    operator_uuid: str | None = Field(
        default=None,
        alias="operatorUuid",
        min_length=1,
        max_length=256,
    )
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LENGTH)
    evidence: dict[str, Any] = Field(default_factory=dict)


class BindToVenueRequest(UncertaintyResolutionRequest):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    venue_offer_id: str = Field(alias="venueOfferId", min_length=1, max_length=256)


class MarkNotAcceptedRequest(UncertaintyResolutionRequest):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ManualResolutionRequest(UncertaintyResolutionRequest):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    operator_uuid: str = Field(alias="operatorUuid", min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=_MAX_REASON_LENGTH)


class UncertaintyResolutionContext(BaseModel):
    """Server-derived, bounded inputs for one audited operator action."""

    model_config = ConfigDict(populate_by_name=True)

    reconcile_event_seq: int | None = Field(
        default=None,
        serialization_alias="reconcileEventSeq",
    )
    query_started_at_ms: int | None = Field(
        default=None,
        serialization_alias="queryStartedAtMs",
    )
    query_finished_at_ms: int | None = Field(
        default=None,
        serialization_alias="queryFinishedAtMs",
    )
    candidate_count: int | None = Field(
        default=None,
        serialization_alias="candidateCount",
    )
    candidate_venue_offer_ids: list[str] = Field(
        default_factory=list,
        serialization_alias="candidateVenueOfferIds",
    )
    unavailable_reason: str | None = Field(
        default=None,
        serialization_alias="unavailableReason",
    )


class UncertaintyResponse(BaseModel):
    """Bounded operator-console uncertainty DTO (never raw event payload)."""

    model_config = ConfigDict(populate_by_name=True)

    uncertainty_id: str = Field(serialization_alias="uncertaintyId")
    kind: str
    symbol: str
    intended_amount: str = Field(serialization_alias="intendedAmount")
    state: Literal["open", "resolved"]
    opened_event_seq: int = Field(serialization_alias="openedEventSeq")
    reconcile_event_seq: int | None = Field(
        default=None,
        serialization_alias="reconcileEventSeq",
    )
    resolved_event_seq: int | None = Field(
        default=None,
        serialization_alias="resolvedEventSeq",
    )
    evidence_summary: dict[str, _EvidenceValue] = Field(
        default_factory=dict,
        serialization_alias="evidenceSummary",
    )
    blocked_scope: dict[str, str] = Field(serialization_alias="blockedScope")
    resolved_by_operator_id: str | None = Field(
        default=None,
        serialization_alias="resolvedByOperatorId",
    )
    resolution_reason: str | None = Field(
        default=None,
        serialization_alias="resolutionReason",
    )
    resolution_context: UncertaintyResolutionContext | None = Field(
        default=None,
        serialization_alias="resolutionContext",
    )


def _http_error(code: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail=code)


def _bounded_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach and cap evidence before it becomes an immutable audit value."""
    try:
        encoded = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if len(encoded.encode("utf-8")) > _MAX_EVIDENCE_BYTES:
            raise ValueError("evidence too large")
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _http_error(
            "invalid_resolution_evidence", status.HTTP_422_UNPROCESSABLE_ENTITY
        ) from exc
    if not isinstance(decoded, dict):  # defensive: JSON object was required
        raise _http_error("invalid_resolution_evidence", status.HTTP_422_UNPROCESSABLE_ENTITY)
    return decoded


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            return value[:256]
        return value
    return None


def _evidence_summary(value: Mapping[str, Any] | None) -> dict[str, _EvidenceValue]:
    """Expose only allowlisted, bounded evidence fields.

    In particular, arbitrary response payloads, credentials, signed headers,
    and request bodies never cross this API boundary.  ``coverage`` is kept as
    a small boolean map because it is useful to an operator without exposing
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
    result: dict[str, _EvidenceValue] = {}
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


def _response(
    row: ExecutionUncertaintyRow,
    *,
    resolution_context: UncertaintyResolutionContext | None = None,
) -> dict[str, object]:
    state = row.state
    if state not in {"open", "resolved"}:
        # A future state is not safe to present as executable.  It can still be
        # read for incident response, but the DTO remains closed and bounded.
        state = "open"
    return UncertaintyResponse(
        uncertainty_id=str(row.uncertainty_id),
        kind=row.kind,
        symbol=row.symbol,
        intended_amount=str(Decimal(str(row.intended_amount))),
        state=state,
        opened_event_seq=row.opened_event_seq,
        reconcile_event_seq=row.reconcile_event_seq,
        resolved_event_seq=row.resolved_event_seq,
        evidence_summary=_evidence_summary(row.evidence),
        blocked_scope={
            "exchangeAccountId": str(row.exchange_account_id),
            "environment": row.deployment_environment,
            "symbol": row.symbol,
        },
        resolved_by_operator_id=row.resolved_by_operator_id,
        resolution_reason=row.resolution_reason,
        resolution_context=resolution_context,
    ).model_dump(by_alias=True)


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _unavailable_context(
    *,
    reconcile_event_seq: int | None = None,
    payload: Mapping[str, Any] | None = None,
    reason: str,
    candidate_count: int | None = None,
    candidate_venue_offer_ids: list[str] | None = None,
) -> UncertaintyResolutionContext:
    return UncertaintyResolutionContext(
        reconcile_event_seq=reconcile_event_seq,
        query_started_at_ms=_optional_int(
            payload.get("query_started_at_ms") if payload is not None else None
        ),
        query_finished_at_ms=_optional_int(
            payload.get("query_finished_at_ms") if payload is not None else None
        ),
        candidate_count=candidate_count,
        candidate_venue_offer_ids=candidate_venue_offer_ids or [],
        unavailable_reason=reason[:256],
    )


async def _load_uncertainty(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    uncertainty_id: UUID,
) -> ExecutionUncertaintyRow:
    row = await _load_scoped_uncertainty(
        session,
        context=context,
        uncertainty_id=uncertainty_id,
    )
    if row.state != "open":
        raise _http_error("uncertainty_already_resolved", status.HTTP_409_CONFLICT)
    if row.kind not in _SUPPORTED_KINDS:
        raise _http_error("unsupported_uncertainty_kind", status.HTTP_409_CONFLICT)
    return row


async def _load_scoped_uncertainty(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    uncertainty_id: UUID,
) -> ExecutionUncertaintyRow:
    row = await session.scalar(
        select(ExecutionUncertaintyRow).where(
            ExecutionUncertaintyRow.uncertainty_id == uncertainty_id,
            ExecutionUncertaintyRow.exchange_account_id == context.exchange_account_id,
            ExecutionUncertaintyRow.deployment_environment == context.deployment_environment,
        )
    )
    if row is None:
        # Missing UUID, another account's UUID, and a retired/non-member account
        # all intentionally have the same non-enumerating response shape.
        raise _http_error("not_found", status.HTTP_404_NOT_FOUND)
    return row


async def _fresh_reconcile(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    row: ExecutionUncertaintyRow,
    reconcile_event_seq: int,
    require_history: bool,
) -> dict[str, Any]:
    if reconcile_event_seq <= row.opened_event_seq:
        raise _http_error("stale_reconcile_fence", status.HTTP_409_CONFLICT)
    reconcile = await session.scalar(
        select(EventLogRow).where(
            EventLogRow.event_seq == reconcile_event_seq,
            EventLogRow.exchange_account_id == context.exchange_account_id,
            EventLogRow.deployment_environment == context.deployment_environment,
            EventLogRow.event_type == _RECONCILE_EVENT_TYPE,
        )
    )
    if reconcile is None or not isinstance(reconcile.payload, dict):
        raise _http_error("stale_reconcile_fence", status.HTTP_409_CONFLICT)
    opening = await session.scalar(
        select(EventLogRow).where(
            EventLogRow.event_seq == row.opened_event_seq,
            EventLogRow.exchange_account_id == context.exchange_account_id,
            EventLogRow.deployment_environment == context.deployment_environment,
        )
    )
    query_started_at_ms = reconcile.payload.get("query_started_at_ms")
    query_finished_at_ms = reconcile.payload.get("query_finished_at_ms")
    if (
        opening is None
        or not isinstance(query_started_at_ms, int)
        or isinstance(query_started_at_ms, bool)
        or not isinstance(query_finished_at_ms, int)
        or isinstance(query_finished_at_ms, bool)
        or query_started_at_ms <= opening.occurred_at_ms
        or query_finished_at_ms < query_started_at_ms
    ):
        raise _http_error("stale_reconcile_fence", status.HTTP_409_CONFLICT)
    # A sequence is a fence only if it is the newest snapshot currently known
    # for this exact account/environment.  This closes the race where an
    # operator submits an old complete snapshot after a newer partial read.
    latest_seq = await session.scalar(
        select(func.max(EventLogRow.event_seq)).where(
            EventLogRow.exchange_account_id == context.exchange_account_id,
            EventLogRow.deployment_environment == context.deployment_environment,
            EventLogRow.event_type == _RECONCILE_EVENT_TYPE,
        )
    )
    if latest_seq != reconcile_event_seq:
        raise _http_error("stale_reconcile_fence", status.HTTP_409_CONFLICT)
    latest_projected_snapshot_at = await session.scalar(
        select(func.max(PositionStateRow.last_venue_snapshot_at)).where(
            PositionStateRow.exchange_account_id == context.exchange_account_id,
            PositionStateRow.deployment_environment == context.deployment_environment,
        )
    )
    if (
        latest_projected_snapshot_at is not None
        and query_finished_at_ms < latest_projected_snapshot_at
    ):
        raise _http_error("stale_reconcile_fence", status.HTTP_409_CONFLICT)
    coverage = reconcile.payload.get("coverage")
    if not isinstance(coverage, dict) or not all(
        bool(coverage.get(key))
        for key in ("active_offers_complete", "active_credits_complete", "wallets_complete")
    ):
        raise _http_error("incomplete_reconcile_coverage", status.HTTP_409_CONFLICT)
    if require_history and not bool(coverage.get("offer_history_complete")):
        raise _http_error("incomplete_offer_history_coverage", status.HTTP_409_CONFLICT)
    return reconcile.payload


async def _resolution_context(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    row: ExecutionUncertaintyRow,
) -> UncertaintyResolutionContext:
    """Describe only actions provable from the latest authoritative snapshot."""
    if row.state != "open":
        return _unavailable_context(reason="uncertainty_not_open")
    latest = await session.scalar(
        select(EventLogRow)
        .where(
            EventLogRow.exchange_account_id == context.exchange_account_id,
            EventLogRow.deployment_environment == context.deployment_environment,
            EventLogRow.event_type == _RECONCILE_EVENT_TYPE,
        )
        .order_by(EventLogRow.event_seq.desc())
        .limit(1)
    )
    if latest is None or not isinstance(latest.payload, dict):
        return _unavailable_context(reason="fresh_reconcile_required")

    latest_payload = latest.payload
    try:
        payload = await _fresh_reconcile(
            session,
            context=context,
            row=row,
            reconcile_event_seq=latest.event_seq,
            require_history=row.kind == "submit_outcome_unknown",
        )
    except HTTPException as exc:
        reason = exc.detail if isinstance(exc.detail, str) else "resolution_context_unavailable"
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=latest_payload,
            reason=reason,
        )

    base = {
        "reconcile_event_seq": latest.event_seq,
        "query_started_at_ms": _optional_int(payload.get("query_started_at_ms")),
        "query_finished_at_ms": _optional_int(payload.get("query_finished_at_ms")),
    }
    if row.kind in {"unattributed_venue_offer", "unsupported_venue_exposure"}:
        return UncertaintyResolutionContext(**base)
    if row.kind != "submit_outcome_unknown":
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason="unsupported_uncertainty_kind",
        )

    try:
        attempt_row = await _load_attempt(session, context=context, row=row)
    except HTTPException as exc:
        reason = exc.detail if isinstance(exc.detail, str) else "submission_attempt_not_resolvable"
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason=reason,
        )
    attempt = attempt_from_row(attempt_row)
    if attempt is None:
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason="submission_attempt_not_resolvable",
        )
    match = match_attempt_to_snapshot(attempt, payload)
    candidate_ids = sorted({offer.venue_offer_id for offer in match.candidates})
    if match.kind == "incomplete":
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason="incomplete_match_evidence",
        )
    if match.kind == "multiple_match":
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason="multiple_exact_candidates",
            candidate_count=len(candidate_ids),
            candidate_venue_offer_ids=candidate_ids,
        )
    return UncertaintyResolutionContext(
        **base,
        candidate_count=len(candidate_ids),
        candidate_venue_offer_ids=candidate_ids,
    )


def _operator_id(context: ExchangeAccountContext, supplied: str | None) -> str:
    value = (supplied or context.user_id).strip()
    if not value:
        raise _http_error("operator_required", status.HTTP_422_UNPROCESSABLE_ENTITY)
    if value != context.user_id:
        # Do not allow an operator to write an audit event under another
        # immutable principal's identity, even if the request is otherwise
        # account-authorized.
        raise _http_error("operator_identity_mismatch", status.HTTP_403_FORBIDDEN)
    return value


def _validate_request_evidence(
    supplied: Mapping[str, Any],
    *,
    allowed: frozenset[str],
) -> None:
    evidence = _bounded_evidence(supplied)
    if not set(evidence).issubset(allowed):
        raise _http_error("invalid_resolution_evidence", status.HTTP_422_UNPROCESSABLE_ENTITY)


def _manual_resolution_action(
    supplied: Mapping[str, Any],
) -> ManualUncertaintyResolutionAction:
    evidence = _bounded_evidence(supplied)
    action = evidence.get("decision")
    if (
        set(evidence) != {"decision"}
        or not isinstance(action, str)
        or action not in MANUAL_UNCERTAINTY_RESOLUTION_ACTIONS
    ):
        raise _http_error(
            "invalid_manual_resolution_decision",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return action


async def _load_attempt(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    row: ExecutionUncertaintyRow,
) -> SubmissionAttemptRow:
    if row.kind != "submit_outcome_unknown" or row.attempt_id is None:
        raise _http_error("resolution_action_not_supported", status.HTTP_409_CONFLICT)
    attempt = await session.scalar(
        select(SubmissionAttemptRow).where(
            SubmissionAttemptRow.attempt_id == row.attempt_id,
            SubmissionAttemptRow.exchange_account_id == context.exchange_account_id,
            SubmissionAttemptRow.deployment_environment == context.deployment_environment,
            SubmissionAttemptRow.symbol == row.symbol,
        )
    )
    if attempt is None or attempt.outcome_kind != "unknown":
        raise _http_error("submission_attempt_not_resolvable", status.HTTP_409_CONFLICT)
    return attempt


async def _append_resolution(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    event: object,
) -> int:
    try:
        result = await AccountEventWriter(
            store=PostgresEventStore(
                deployment_environment=context.deployment_environment,
            )
        ).append(session, event)
    except ProjectionWriteError as exc:
        raise _http_error("resolution_rejected", status.HTTP_409_CONFLICT) from exc
    except (ValueError, TypeError) as exc:
        raise _http_error("resolution_rejected", status.HTTP_409_CONFLICT) from exc
    return result.event_seq


def build_uncertainties_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["uncertainties"],
        dependencies=[Depends(shared_rate_limit_dependency())],
    )

    @router.get("/exchange-accounts/{exchange_account_id}/uncertainties")
    async def list_account_uncertainties(
        state: Literal["open", "resolved"] | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        stmt = (
            select(ExecutionUncertaintyRow)
            .where(
                ExecutionUncertaintyRow.exchange_account_id == context.exchange_account_id,
                ExecutionUncertaintyRow.deployment_environment == context.deployment_environment,
            )
            .order_by(ExecutionUncertaintyRow.opened_event_seq.desc())
            .limit(limit)
        )
        if state is not None:
            stmt = stmt.where(ExecutionUncertaintyRow.state == state)
        try:
            rows = (await session.execute(stmt)).scalars().all()
            responses = [
                _response(
                    row,
                    resolution_context=await _resolution_context(
                        session,
                        context=context,
                        row=row,
                    ),
                )
                for row in rows
            ]
        except Exception as exc:
            raise _http_error(
                "uncertainties_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
            ) from exc
        return {"data": responses}

    @router.get("/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}")
    async def get_account_uncertainty(
        uncertainty_id: UUID,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        row = await _load_scoped_uncertainty(
            session,
            context=context,
            uncertainty_id=uncertainty_id,
        )
        try:
            resolution_context = await _resolution_context(
                session,
                context=context,
                row=row,
            )
        except Exception as exc:
            raise _http_error(
                "uncertainties_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
            ) from exc
        return {
            "data": _response(row, resolution_context=resolution_context),
        }

    @router.post(
        "/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/bind-to-venue"
    )
    async def bind_to_venue(
        uncertainty_id: UUID,
        body: BindToVenueRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        _validate_request_evidence(
            body.evidence,
            allowed=frozenset({"candidateCount", "candidate_count"}),
        )
        row = await _load_uncertainty(session, context=context, uncertainty_id=uncertainty_id)
        payload = await _fresh_reconcile(
            session,
            context=context,
            row=row,
            reconcile_event_seq=body.reconcile_event_seq,
            require_history=True,
        )
        attempt_row = await _load_attempt(session, context=context, row=row)
        attempt = attempt_from_row(attempt_row)
        match = match_attempt_to_snapshot(attempt, payload) if attempt is not None else None
        if (
            match is None
            or match.kind != "exact_match"
            or match.offer is None
            or match.offer.venue_offer_id != body.venue_offer_id
        ):
            raise _http_error("venue_offer_match_not_exact", status.HTTP_409_CONFLICT)
        operator_id = _operator_id(context, body.operator_uuid)
        evidence = deterministic_resolution_evidence(
            reconcile_event_seq=body.reconcile_event_seq,
            payload=payload,
            candidate_count=1,
            venue_offer_id=body.venue_offer_id,
        )
        event_seq = await _append_resolution(
            session,
            context=context,
            event=UncertaintyBoundToVenueOffer(
                uncertainty_id=row.uncertainty_id,
                account_id=str(context.exchange_account_id),
                environment=context.deployment_environment,
                symbol=row.symbol,
                kind=row.kind,
                venue_offer_id=body.venue_offer_id,
                reconcile_event_seq=body.reconcile_event_seq,
                resolved_by_operator_id=operator_id,
                resolution_reason=(body.reason or "bind_to_venue_offer").strip(),
                resolution_evidence=evidence,
                venue_status=match.offer.status,
                occurred_at_ms=int(time.time() * 1000),
            ),
        )
        return {"data": {**_response(row), "resolvedEventSeq": event_seq, "state": "resolved"}}

    @router.post(
        "/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/mark-not-accepted"
    )
    async def mark_not_accepted(
        uncertainty_id: UUID,
        body: MarkNotAcceptedRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        _validate_request_evidence(
            body.evidence,
            allowed=frozenset({"candidateCount", "candidate_count"}),
        )
        row = await _load_uncertainty(session, context=context, uncertainty_id=uncertainty_id)
        payload = await _fresh_reconcile(
            session,
            context=context,
            row=row,
            reconcile_event_seq=body.reconcile_event_seq,
            require_history=True,
        )
        if row.kind != "submit_outcome_unknown":
            raise _http_error("resolution_action_not_supported", status.HTTP_409_CONFLICT)
        attempt_row = await _load_attempt(session, context=context, row=row)
        attempt = attempt_from_row(attempt_row)
        match = match_attempt_to_snapshot(attempt, payload) if attempt is not None else None
        if match is None or match.kind != "zero_match":
            raise _http_error("venue_offer_match_not_zero", status.HTTP_409_CONFLICT)
        operator_id = _operator_id(context, body.operator_uuid)
        evidence = deterministic_resolution_evidence(
            reconcile_event_seq=body.reconcile_event_seq,
            payload=payload,
            candidate_count=0,
        )
        event_seq = await _append_resolution(
            session,
            context=context,
            event=UncertaintyMarkedNotAccepted(
                uncertainty_id=row.uncertainty_id,
                account_id=str(context.exchange_account_id),
                environment=context.deployment_environment,
                symbol=row.symbol,
                kind=row.kind,
                reconcile_event_seq=body.reconcile_event_seq,
                resolved_by_operator_id=operator_id,
                resolution_reason=(body.reason or "confirmed_not_accepted").strip(),
                resolution_evidence=evidence,
                candidate_count=0,
                occurred_at_ms=int(time.time() * 1000),
            ),
        )
        return {"data": {**_response(row), "resolvedEventSeq": event_seq, "state": "resolved"}}

    @router.post(
        "/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/manual-resolution"
    )
    async def manual_resolution(
        uncertainty_id: UUID,
        body: ManualResolutionRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        _validate_request_evidence(body.evidence, allowed=frozenset({"decision"}))
        row = await _load_uncertainty(session, context=context, uncertainty_id=uncertainty_id)
        if row.kind not in {"unattributed_venue_offer", "unsupported_venue_exposure"}:
            raise _http_error("resolution_action_not_supported", status.HTTP_409_CONFLICT)
        resolution_action = _manual_resolution_action(body.evidence)
        payload = await _fresh_reconcile(
            session,
            context=context,
            row=row,
            reconcile_event_seq=body.reconcile_event_seq,
            require_history=False,
        )
        operator_id = _operator_id(context, body.operator_uuid)
        evidence = deterministic_resolution_evidence(
            reconcile_event_seq=body.reconcile_event_seq,
            payload=payload,
        )
        event_seq = await _append_resolution(
            session,
            context=context,
            event=UncertaintyManuallyResolved(
                uncertainty_id=row.uncertainty_id,
                account_id=str(context.exchange_account_id),
                environment=context.deployment_environment,
                symbol=row.symbol,
                kind=row.kind,
                reconcile_event_seq=body.reconcile_event_seq,
                resolved_by_operator_id=operator_id,
                resolution_reason=body.reason.strip(),
                resolution_action=resolution_action,
                resolution_evidence=evidence,
                occurred_at_ms=int(time.time() * 1000),
            ),
        )
        return {"data": {**_response(row), "resolvedEventSeq": event_seq, "state": "resolved"}}

    return router


__all__ = [
    "BindToVenueRequest",
    "ManualResolutionRequest",
    "MarkNotAcceptedRequest",
    "UncertaintyResolutionRequest",
    "UncertaintyResponse",
    "build_uncertainties_router",
]
