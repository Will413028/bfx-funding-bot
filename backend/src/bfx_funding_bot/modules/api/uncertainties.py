"""Account-scoped operator reads of execution uncertainties.

The rows returned by this module are deliberately small, bounded read DTOs.
Adjudicating an uncertainty goes through the operator CLI only (ADR 2026-10-15 D4):
``bfx-op bind-to-venue|mark-not-accepted|manual-resolution`` queues the request
(``execution.operator_commands``), and the account daemon's resolution worker
re-validates it and alone appends the domain event. This service holds no write on
the adjudication requests (36e59526f8cf), the ledger or the projections; it shows a
queued request and its outcome (``GET .../uncertainty-resolution-requests/{id}``,
and the newest one on each uncertainty).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    command_error,
    require_account_member,
)
from bfx_funding_bot.modules.api.deps import ReadModels, get_read_models, get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import Envelope, ResponseModel
from bfx_funding_bot.modules.execution import operator_commands as commands
from bfx_funding_bot.modules.execution.operator_commands import (
    OperatorCommandError,
    UncertaintyDetail,
)
from bfx_funding_bot.modules.execution.operator_requests import RequestState
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ResolutionAction,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    Scope,
    UncertaintyKind,
    UncertaintyState,
    observation_evidence_ref,
)

_MAX_LIMIT = 100
_MAX_CANDIDATE_VENUE_OFFER_IDS = 16
_EvidenceValue = str | int | float | bool | None | dict[str, Any] | list[Any]


class UncertaintyResolutionContext(ResponseModel):
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


class BlockedScope(ResponseModel):
    """The account, environment and symbol an open uncertainty blocks."""

    exchange_account_id: str = Field(serialization_alias="exchangeAccountId")
    environment: str
    symbol: str


class ResolutionRequestResponse(ResponseModel):
    """One queued adjudication and, once the daemon has settled it, its outcome."""

    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(serialization_alias="requestId")
    uncertainty_id: str = Field(serialization_alias="uncertaintyId")
    action: ResolutionAction
    state: RequestState
    evidence_ref: str = Field(serialization_alias="evidenceRef")
    created_at_ms: int = Field(serialization_alias="createdAtMs")
    processed_at_ms: int | None = Field(default=None, serialization_alias="processedAtMs")
    outcome_reason: str | None = Field(default=None, serialization_alias="outcomeReason")


class UncertaintyResponse(ResponseModel):
    """Bounded operator-console uncertainty DTO (never raw event payload)."""

    model_config = ConfigDict(populate_by_name=True)

    uncertainty_id: str = Field(serialization_alias="uncertaintyId")
    kind: UncertaintyKind
    symbol: str
    intended_amount: str = Field(serialization_alias="intendedAmount")
    state: UncertaintyState
    evidence_summary: dict[str, _EvidenceValue] = Field(
        default_factory=dict,
        serialization_alias="evidenceSummary",
    )
    blocked_scope: BlockedScope = Field(serialization_alias="blockedScope")
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


def _response(detail: UncertaintyDetail, scope: Scope) -> UncertaintyResponse:
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
        blocked_scope=BlockedScope(
            exchange_account_id=str(scope.exchange_account_id),
            environment=scope.deployment_environment,
            symbol=row.symbol,
        ),
        resolved_by_operator_id=row.resolved_by_operator_id,
        resolution_reason=row.resolution_reason,
        resolution_context=_resolution_context(detail),
        resolution_request=(
            _request_model(detail.latest_request) if detail.latest_request is not None else None
        ),
    )


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


def _rendered[T](render: Callable[[], T]) -> T:
    """A read's response; one that cannot be rendered is unavailable, never a 500."""
    try:
        return render()
    except Exception as exc:
        raise command_error(OperatorCommandError(
            "uncertainties_unavailable", kind="unavailable")) from exc


def build_uncertainties_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["uncertainties"],
        dependencies=[Depends(shared_rate_limit_dependency())],
    )

    @router.get("/exchange-accounts/{exchange_account_id}/uncertainties")
    async def list_account_uncertainties(
        state: Annotated[UncertaintyState | None, Query()] = None,
        limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> Envelope[list[UncertaintyResponse]]:
        try:
            details = await commands.list_uncertainties(
                session, context, models.operator_reads, models.operator_evidence,
                state=state, limit=limit)
        except OperatorCommandError as exc:
            raise command_error(exc) from exc
        return Envelope(data=_rendered(lambda: [_response(d, context.scope) for d in details]))

    @router.get("/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}")
    async def get_account_uncertainty(
        uncertainty_id: UUID,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        models: ReadModels = Depends(get_read_models),  # noqa: B008
    ) -> Envelope[UncertaintyResponse]:
        try:
            detail = await commands.get_uncertainty(
                session, context, models.operator_reads, models.operator_evidence,
                uncertainty_id)
        except OperatorCommandError as exc:
            raise command_error(exc) from exc
        return Envelope(data=_rendered(lambda: _response(detail, context.scope)))

    @router.get(
        "/exchange-accounts/{exchange_account_id}/uncertainty-resolution-requests/{request_id}"
    )
    async def get_resolution_request(
        request_id: UUID,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Envelope[ResolutionRequestResponse]:
        try:
            row = await commands.get_resolution_request(session, context, request_id)
        except OperatorCommandError as exc:
            raise command_error(exc) from exc
        return Envelope(data=_request_model(row))

    return router


__all__ = [
    "ResolutionRequestResponse",
    "UncertaintyResponse",
    "build_uncertainties_router",
]
