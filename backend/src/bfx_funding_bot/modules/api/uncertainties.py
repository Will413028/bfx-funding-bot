"""Account-scoped operator endpoints for execution uncertainties.

The rows returned by this module are deliberately small, bounded read DTOs.
Resolution is an operator request, not a write (ADR D4'): the handler validates
the account scope and reconcile fence so the operator sees a refusal at once,
then queues the request. The account daemon's resolution worker re-validates it
and alone appends the domain event, so this service holds no ledger or
projection write privilege.
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
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    require_account_member,
    require_account_write,
)
from bfx_funding_bot.modules.api.deps import ReadModels, get_read_models, get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionAction,
    ResolutionRejected,
    ResolutionScope,
    UncertaintyResolutionRequests,
    request_rejection,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import UncertaintyResolutionRequestRow
from bfx_funding_bot.modules.ledger import (
    OperatorEvidence,
    OperatorResolution,
    ResolutionIntent,
    ResolutionSubject,
    Scope,
    UncertaintyView,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger import ResolutionRejected as EvidenceRejected

_MAX_LIMIT = 100
_MAX_REASON_LENGTH = 512
_MAX_EVIDENCE_BYTES = 16_384
_MAX_CANDIDATE_VENUE_OFFER_IDS = 16
_EvidenceValue = str | int | float | bool | None | dict[str, Any] | list[Any]
_REJECTION_STATUS = {
    "conflict": status.HTTP_409_CONFLICT,
    "invalid": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "not_found": status.HTTP_404_NOT_FOUND,
}


class UncertaintyResolutionRequest(BaseModel):
    """Common opaque evidence reference and optional operator identity."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    evidence_ref: str = Field(alias="evidenceRef")
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

    evidence_ref: str | None = Field(default=None, serialization_alias="evidenceRef")
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
        max_length=_MAX_CANDIDATE_VENUE_OFFER_IDS,
    )
    unavailable_reason: str | None = Field(
        default=None,
        serialization_alias="unavailableReason",
    )


class ResolutionRequestResponse(BaseModel):
    """One queued adjudication and, once the daemon has settled it, its outcome."""

    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(serialization_alias="requestId")
    uncertainty_id: str = Field(serialization_alias="uncertaintyId")
    action: str
    state: Literal["requested", "applied", "rejected", "failed"]
    evidence_ref: str = Field(serialization_alias="evidenceRef")
    created_at_ms: int = Field(serialization_alias="createdAtMs")
    processed_at_ms: int | None = Field(default=None, serialization_alias="processedAtMs")
    outcome_reason: str | None = Field(default=None, serialization_alias="outcomeReason")


class UncertaintyResponse(BaseModel):
    """Bounded operator-console uncertainty DTO (never raw event payload)."""

    model_config = ConfigDict(populate_by_name=True)

    uncertainty_id: str = Field(serialization_alias="uncertaintyId")
    kind: str
    symbol: str
    intended_amount: str = Field(serialization_alias="intendedAmount")
    state: Literal["open", "resolved"]
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
    # The newest adjudication request: pending while the daemon has yet to apply
    # it, else its outcome. The console reads status from here alone.
    resolution_request: ResolutionRequestResponse | None = Field(
        default=None,
        serialization_alias="resolutionRequest",
    )


def _http_error(code: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail=code)


def _rejected(exc: ResolutionRejected) -> HTTPException:
    return _http_error(exc.code, _REJECTION_STATUS[exc.kind])


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
    row: UncertaintyView,
    scope: Scope,
    *,
    resolution_context: UncertaintyResolutionContext | None = None,
    latest_request: UncertaintyResolutionRequestRow | None = None,
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
        evidence_summary=_evidence_summary(row.evidence),
        blocked_scope={
            "exchangeAccountId": str(scope.exchange_account_id),
            "environment": scope.deployment_environment,
            "symbol": row.symbol,
        },
        resolved_by_operator_id=row.resolved_by_operator_id,
        resolution_reason=row.resolution_reason,
        resolution_context=resolution_context,
        resolution_request=(
            _request_model(latest_request) if latest_request is not None else None
        ),
    ).model_dump(by_alias=True)


def _request_model(row: UncertaintyResolutionRequestRow) -> ResolutionRequestResponse:
    return ResolutionRequestResponse(
        request_id=str(row.request_id),
        uncertainty_id=str(row.uncertainty_id),
        action=row.action,
        state=row.state,
        evidence_ref=observation_evidence_ref(row.observation_id),
        created_at_ms=row.created_at_ms,
        processed_at_ms=row.processed_at_ms,
        outcome_reason=row.outcome_reason,
    )


def _request_response(row: UncertaintyResolutionRequestRow) -> dict[str, object]:
    return _request_model(row).model_dump(by_alias=True)


def _scope(context: ExchangeAccountContext) -> ResolutionScope:
    return ResolutionScope(context.exchange_account_id, context.deployment_environment)


def _read_scope(context: ExchangeAccountContext) -> Scope:
    return Scope(context.exchange_account_id, context.deployment_environment)


async def _resolution_context(
    session: AsyncSession, *, context: ExchangeAccountContext, row: UncertaintyView,
    evidence: OperatorEvidence,
) -> UncertaintyResolutionContext:
    result = await evidence.resolution_context(
        session, _read_scope(context),
        ResolutionSubject(row.uncertainty_id, row.symbol, row.attempt_id),
    )
    return UncertaintyResolutionContext(
        evidence_ref=result.evidence_ref,
        query_started_at_ms=result.query_started_at_ms,
        query_finished_at_ms=result.query_finished_at_ms,
        candidate_count=result.candidate_count,
        candidate_venue_offer_ids=list(result.candidate_venue_offer_ids),
        unavailable_reason=result.unavailable_reason,
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
) -> dict[str, Any]:
    evidence = _bounded_evidence(supplied)
    if not set(evidence).issubset(allowed):
        raise _http_error("invalid_resolution_evidence", status.HTTP_422_UNPROCESSABLE_ENTITY)
    return evidence


def _manual_decision(evidence: Mapping[str, Any]) -> str | None:
    """The decision string, or None; the allow-list check happens after kind.

    Keeping the value check in the shared validator preserves the code an
    operator sees when both the kind and the decision are wrong.
    """
    decision = evidence.get("decision")
    if set(evidence) != {"decision"} or not isinstance(decision, str):
        return None
    return decision


async def _queue(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    uncertainty_id: UUID,
    action: ResolutionAction,
    body: UncertaintyResolutionRequest,
    venue_offer_id: str | None = None,
    decision: str | None = None,
    resolution: OperatorResolution,
) -> dict[str, object]:
    try:
        # The reference is judged before who asked, as it always was.
        resolution.columns(body.evidence_ref)
    except EvidenceRejected as exc:
        raise _rejected(request_rejection(exc)) from exc
    intent = ResolutionIntent(
        uncertainty_id=uncertainty_id,
        action=action,
        evidence_ref=body.evidence_ref,
        operator_id=_operator_id(context, body.operator_uuid),
        reason=body.reason,
        venue_offer_id=venue_offer_id,
        decision=decision,
    )
    try:
        row = await UncertaintyResolutionRequests(_scope(context), resolution).request(
            session, intent, now_ms=int(time.time() * 1000)
        )
    except ResolutionRejected as exc:
        raise _rejected(exc) from exc
    return {"data": _request_response(row)}


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
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        scope = _read_scope(context)
        try:
            rows = await models.operator_reads.list_uncertainties(
                session, scope, state=state, limit=limit
            )
            latest = await UncertaintyResolutionRequests(_scope(context)).latest_for(
                session, [row.uncertainty_id for row in rows]
            )
            responses = [
                _response(
                    row,
                    scope,
                    resolution_context=await _resolution_context(
                        session,
                        context=context,
                        row=row,
                        evidence=models.operator_evidence,
                    ),
                    latest_request=latest.get(row.uncertainty_id),
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
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        scope = _read_scope(context)
        row = await models.operator_reads.get_uncertainty(session, scope, uncertainty_id)
        if row is None:
            raise _rejected(ResolutionRejected("not_found", kind="not_found"))
        try:
            resolution_context = await _resolution_context(
                session,
                context=context,
                row=row,
                evidence=models.operator_evidence,
            )
            latest = await UncertaintyResolutionRequests(_scope(context)).latest_for(
                session, [row.uncertainty_id]
            )
        except Exception as exc:
            raise _http_error(
                "uncertainties_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
            ) from exc
        return {
            "data": _response(
                row,
                scope,
                resolution_context=resolution_context,
                latest_request=latest.get(row.uncertainty_id),
            ),
        }

    @router.get(
        "/exchange-accounts/{exchange_account_id}/uncertainty-resolution-requests/{request_id}"
    )
    async def get_resolution_request(
        request_id: UUID,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        try:
            row = await UncertaintyResolutionRequests(_scope(context)).get(session, request_id)
        except ResolutionRejected as exc:
            raise _rejected(exc) from exc
        return {"data": _request_response(row)}

    @router.post(
        "/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/bind-to-venue",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def bind_to_venue(
        uncertainty_id: UUID,
        body: BindToVenueRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        _validate_request_evidence(
            body.evidence,
            allowed=frozenset({"candidateCount", "candidate_count"}),
        )
        return await _queue(
            session,
            context=context,
            uncertainty_id=uncertainty_id,
            action="bind_to_venue",
            body=body,
            resolution=models.operator_resolution,
            venue_offer_id=body.venue_offer_id,
        )

    @router.post(
        "/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/mark-not-accepted",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def mark_not_accepted(
        uncertainty_id: UUID,
        body: MarkNotAcceptedRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        _validate_request_evidence(
            body.evidence,
            allowed=frozenset({"candidateCount", "candidate_count"}),
        )
        return await _queue(
            session,
            context=context,
            uncertainty_id=uncertainty_id,
            action="mark_not_accepted",
            body=body,
            resolution=models.operator_resolution,
        )

    @router.post(
        "/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/manual-resolution",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def manual_resolution(
        uncertainty_id: UUID,
        body: ManualResolutionRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        evidence = _validate_request_evidence(body.evidence, allowed=frozenset({"decision"}))
        return await _queue(
            session,
            context=context,
            uncertainty_id=uncertainty_id,
            action="manual_resolution",
            body=body,
            resolution=models.operator_resolution,
            decision=_manual_decision(evidence),
        )

    return router


__all__ = [
    "BindToVenueRequest",
    "ManualResolutionRequest",
    "MarkNotAcceptedRequest",
    "ResolutionRequestResponse",
    "UncertaintyResolutionRequest",
    "UncertaintyResponse",
    "build_uncertainties_router",
]
