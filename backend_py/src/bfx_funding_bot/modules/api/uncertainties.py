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
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.event_store.writer import (
    AccountEventWriter,
    ProjectionWriteError,
)
from bfx_funding_bot.modules.execution.events import (
    UncertaintyBoundToVenueOffer,
    UncertaintyManuallyResolved,
    UncertaintyMarkedNotAccepted,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow

_MAX_LIMIT = 100
_MAX_REASON_LENGTH = 512
_MAX_EVIDENCE_BYTES = 16_384
_RECONCILE_EVENT_TYPE = "VENUE_SNAPSHOT_OBSERVED"
_SUPPORTED_KINDS = frozenset({
    "submit_outcome_unknown",
    "unattributed_venue_offer",
    "unsupported_venue_exposure",
})
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


def _http_error(code: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail=code)


def _bounded_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach and cap evidence before it becomes an immutable audit value."""
    try:
        encoded = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        if len(encoded.encode("utf-8")) > _MAX_EVIDENCE_BYTES:
            raise ValueError("evidence too large")
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _http_error("invalid_resolution_evidence", status.HTTP_422_UNPROCESSABLE_ENTITY) from exc
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


def _response(row: ExecutionUncertaintyRow) -> dict[str, object]:
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
    ).model_dump(by_alias=True)


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
    coverage = reconcile.payload.get("coverage")
    if not isinstance(coverage, dict) or not all(
        bool(coverage.get(key))
        for key in ("active_offers_complete", "active_credits_complete", "wallets_complete")
    ):
        raise _http_error("incomplete_reconcile_coverage", status.HTTP_409_CONFLICT)
    if require_history and not bool(coverage.get("offer_history_complete")):
        raise _http_error("incomplete_offer_history_coverage", status.HTTP_409_CONFLICT)
    return reconcile.payload


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


def _candidate_offers(payload: Mapping[str, Any], *, symbol: str, venue_offer_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    # The same active object can appear at the boundary of the active and
    # history queries.  Its venue ID is the identity, so that cross-source
    # duplicate is one candidate; distinct records with another ID cannot be
    # bound by this endpoint because the request names one exact ID.
    seen: set[str] = set()
    for source in ("offers", "offer_history"):
        values = payload.get(source, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            if str(value.get("venue_offer_id")) != venue_offer_id:
                continue
            if value.get("symbol") != symbol:
                continue
            if venue_offer_id not in seen:
                candidates.append(value)
                seen.add(venue_offer_id)
    return candidates


def _candidate_count(evidence: Mapping[str, Any]) -> int | None:
    value = evidence.get("candidate_count", evidence.get("candidateCount"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _resolution_evidence(
    supplied: Mapping[str, Any], *, reconcile_event_seq: int, candidate_count: int | None = None,
) -> dict[str, Any]:
    evidence = _bounded_evidence(supplied)
    evidence["reconcile_event_seq"] = reconcile_event_seq
    if candidate_count is not None:
        evidence["candidate_count"] = candidate_count
    return _bounded_evidence(evidence)


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
        stmt = select(ExecutionUncertaintyRow).where(
            ExecutionUncertaintyRow.exchange_account_id == context.exchange_account_id,
            ExecutionUncertaintyRow.deployment_environment == context.deployment_environment,
        ).order_by(ExecutionUncertaintyRow.opened_event_seq.desc()).limit(limit)
        if state is not None:
            stmt = stmt.where(ExecutionUncertaintyRow.state == state)
        try:
            rows = (await session.execute(stmt)).scalars().all()
        except Exception as exc:
            raise _http_error("uncertainties_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE) from exc
        return {"data": [_response(row) for row in rows]}

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
        return {"data": _response(row)}

    @router.post("/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/bind-to-venue")
    async def bind_to_venue(
        uncertainty_id: UUID,
        body: BindToVenueRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        row = await _load_uncertainty(session, context=context, uncertainty_id=uncertainty_id)
        payload = await _fresh_reconcile(
            session,
            context=context,
            row=row,
            reconcile_event_seq=body.reconcile_event_seq,
            require_history=False,
        )
        candidates = _candidate_offers(payload, symbol=row.symbol, venue_offer_id=body.venue_offer_id)
        if len(candidates) != 1:
            raise _http_error("venue_offer_match_not_exact", status.HTTP_409_CONFLICT)
        operator_id = _operator_id(context, body.operator_uuid)
        evidence = _resolution_evidence(
            body.evidence,
            reconcile_event_seq=body.reconcile_event_seq,
            candidate_count=1,
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
                venue_status=str(candidates[0].get("status") or "active"),
                occurred_at_ms=int(time.time() * 1000),
            ),
        )
        return {"data": {**_response(row), "resolvedEventSeq": event_seq, "state": "resolved"}}

    @router.post("/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/mark-not-accepted")
    async def mark_not_accepted(
        uncertainty_id: UUID,
        body: MarkNotAcceptedRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        row = await _load_uncertainty(session, context=context, uncertainty_id=uncertainty_id)
        await _fresh_reconcile(
            session,
            context=context,
            row=row,
            reconcile_event_seq=body.reconcile_event_seq,
            require_history=True,
        )
        if row.kind != "submit_outcome_unknown":
            raise _http_error("resolution_action_not_supported", status.HTTP_409_CONFLICT)
        count = _candidate_count(body.evidence)
        if count != 0:
            raise _http_error("zero_candidate_evidence_required", status.HTTP_409_CONFLICT)
        # Preserve a caller-supplied evidence object only after its candidate
        # count has been checked; the event projector repeats this invariant.
        operator_id = _operator_id(context, body.operator_uuid)
        evidence = _resolution_evidence(
            body.evidence,
            reconcile_event_seq=body.reconcile_event_seq,
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

    @router.post("/exchange-accounts/{exchange_account_id}/uncertainties/{uncertainty_id}/manual-resolution")
    async def manual_resolution(
        uncertainty_id: UUID,
        body: ManualResolutionRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        row = await _load_uncertainty(session, context=context, uncertainty_id=uncertainty_id)
        await _fresh_reconcile(
            session,
            context=context,
            row=row,
            reconcile_event_seq=body.reconcile_event_seq,
            require_history=False,
        )
        operator_id = _operator_id(context, body.operator_uuid)
        evidence = _resolution_evidence(
            body.evidence,
            reconcile_event_seq=body.reconcile_event_seq,
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
