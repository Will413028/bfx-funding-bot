"""Account-scoped operator endpoints for execution uncertainties.

The rows returned by this module are deliberately small, bounded read DTOs.
Resolution is an operator request, not a write (ADR D4'): the handler hands it to
``execution.operator_commands`` (shared with the operator CLI), which validates the
account scope, evidence and reconcile fence so the operator sees a refusal at once,
then queues the request. The account daemon's resolution worker re-validates it
and alone appends the domain event, so this service holds no ledger or
projection write privilege.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    command_error,
    require_account_member,
)
from bfx_funding_bot.modules.api.deps import ReadModels, get_read_models, get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.execution import operator_commands as commands
from bfx_funding_bot.modules.execution.operator_commands import (
    OPERATOR_ID_MAX,
    RESOLUTION_REASON_MAX,
    VENUE_OFFER_ID_MAX,
    OperatorCommandError,
    ResolutionCommand,
    UncertaintyDetail,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ResolutionAction,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import Scope, observation_evidence_ref

_MAX_LIMIT = 100
_MAX_CANDIDATE_VENUE_OFFER_IDS = 16
_EvidenceValue = str | int | float | bool | None | dict[str, Any] | list[Any]


class UncertaintyResolutionRequest(BaseModel):
    """Common opaque evidence reference and optional operator identity."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    evidence_ref: str = Field(alias="evidenceRef")
    operator_uuid: str | None = Field(
        default=None,
        alias="operatorUuid",
        min_length=1,
        max_length=OPERATOR_ID_MAX,
    )
    reason: str | None = Field(default=None, max_length=RESOLUTION_REASON_MAX)
    evidence: dict[str, Any] = Field(default_factory=dict)


class BindToVenueRequest(UncertaintyResolutionRequest):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    venue_offer_id: str = Field(alias="venueOfferId", min_length=1, max_length=VENUE_OFFER_ID_MAX)


class MarkNotAcceptedRequest(UncertaintyResolutionRequest):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ManualResolutionRequest(UncertaintyResolutionRequest):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    operator_uuid: str = Field(alias="operatorUuid", min_length=1, max_length=OPERATOR_ID_MAX)
    reason: str = Field(min_length=1, max_length=RESOLUTION_REASON_MAX)


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


def _resolution_context(detail: UncertaintyDetail) -> UncertaintyResolutionContext:
    result = detail.context
    return UncertaintyResolutionContext(
        evidence_ref=result.evidence_ref,
        query_started_at_ms=result.query_started_at_ms,
        query_finished_at_ms=result.query_finished_at_ms,
        candidate_count=result.candidate_count,
        candidate_venue_offer_ids=list(result.candidate_venue_offer_ids),
        unavailable_reason=result.unavailable_reason,
    )


def _response(detail: UncertaintyDetail, scope: Scope) -> dict[str, object]:
    row = detail.view
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
        evidence_summary=commands.evidence_summary(row.evidence),
        blocked_scope={
            "exchangeAccountId": str(scope.exchange_account_id),
            "environment": scope.deployment_environment,
            "symbol": row.symbol,
        },
        resolved_by_operator_id=row.resolved_by_operator_id,
        resolution_reason=row.resolution_reason,
        resolution_context=_resolution_context(detail),
        resolution_request=(
            _request_model(detail.latest_request) if detail.latest_request is not None else None
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



def _rendered[T](render: Callable[[], T]) -> T:
    """A read's response; one that cannot be rendered is unavailable, never a 500."""
    try:
        return render()
    except Exception as exc:
        raise command_error(OperatorCommandError(
            "uncertainties_unavailable", kind="unavailable")) from exc


async def _queue(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    models: ReadModels,
    uncertainty_id: UUID,
    action: ResolutionAction,
    body: UncertaintyResolutionRequest,
    venue_offer_id: str | None = None,
) -> dict[str, object]:
    command = ResolutionCommand(
        uncertainty_id=uncertainty_id, action=action, evidence_ref=body.evidence_ref,
        operator_uuid=body.operator_uuid, reason=body.reason, evidence=body.evidence,
        venue_offer_id=venue_offer_id,
    )
    try:
        row = await commands.request_resolution(
            session, context, models.operator_resolution, command,
            now_ms=int(time.time() * 1000))
    except OperatorCommandError as exc:
        raise command_error(exc) from exc
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
        try:
            details = await commands.list_uncertainties(
                session, context, models.operator_reads, models.operator_evidence,
                state=state, limit=limit)
        except OperatorCommandError as exc:
            raise command_error(exc) from exc
        return {"data": _rendered(lambda: [_response(d, context.scope) for d in details])}

    @router.get("/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}")
    async def get_account_uncertainty(
        uncertainty_id: UUID,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> dict[str, object]:
        try:
            detail = await commands.get_uncertainty(
                session, context, models.operator_reads, models.operator_evidence,
                uncertainty_id)
        except OperatorCommandError as exc:
            raise command_error(exc) from exc
        return {"data": _rendered(lambda: _response(detail, context.scope))}

    @router.get(
        "/exchange-accounts/{exchange_account_id}/uncertainty-resolution-requests/{request_id}"
    )
    async def get_resolution_request(
        request_id: UUID,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        try:
            row = await commands.get_resolution_request(session, context, request_id)
        except OperatorCommandError as exc:
            raise command_error(exc) from exc
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
        return await _queue(
            session, context=context, models=models, uncertainty_id=uncertainty_id,
            action="bind_to_venue", body=body, venue_offer_id=body.venue_offer_id,
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
        return await _queue(
            session, context=context, models=models, uncertainty_id=uncertainty_id,
            action="mark_not_accepted", body=body,
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
        return await _queue(
            session, context=context, models=models, uncertainty_id=uncertainty_id,
            action="manual_resolution", body=body,
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
