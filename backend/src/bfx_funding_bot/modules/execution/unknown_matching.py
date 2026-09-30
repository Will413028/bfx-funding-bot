"""Pure, fail-closed matching for UNKNOWN funding-offer submissions.

The immutable submission attempt and a persisted venue snapshot are the only
authorities accepted here.  Both boot recovery and operator resolution use
this module so live matching and event-log replay cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    FundingOfferHistoryCoverage,
)
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.submit_outcomes import (
    VENUE_CLOCK_TOLERANCE_MS,
    fingerprint_submit_payload,
)


@dataclass(frozen=True, slots=True)
class UnknownSubmitAttempt:
    attempt_id: UUID
    execution_decision_id: str
    account_id: str
    symbol: str
    cid: int
    amount: Decimal
    rate: Decimal
    period_days: int
    offer_type: str
    flags: Mapping[str, Any] | int
    started_at_ms: int
    signal_correlation_id: UUID
    reservation_ref: ReservationRef


@dataclass(frozen=True, slots=True)
class MatchResult:
    kind: Literal["exact_match", "zero_match", "multiple_match", "incomplete"]
    offer: ActiveFundingOffer | None = None
    candidates: tuple[ActiveFundingOffer, ...] = ()


def normalized_match_flags(value: Mapping[str, Any] | int) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {"raw": value}


def offer_matches_attempt_identity(
    attempt: UnknownSubmitAttempt,
    offer: ActiveFundingOffer,
    *,
    observed_end_ms: int,
) -> bool:
    original = offer.amount_original if offer.amount_original is not None else offer.amount
    return (
        offer.symbol == attempt.symbol
        and original == attempt.amount
        and offer.rate_observed
        and offer.rate_decimal is not None
        and offer.rate_decimal == attempt.rate
        and offer.period_days == attempt.period_days
        and attempt.started_at_ms - VENUE_CLOCK_TOLERANCE_MS
        <= offer.mts_created <= observed_end_ms
        and offer.offer_type is not None
        and offer.offer_type == attempt.offer_type
        and offer.flags is not None
        and normalized_match_flags(offer.flags) == normalized_match_flags(attempt.flags)
    )


def _coverage_invariants_hold(coverage: FundingOfferHistoryCoverage) -> bool:
    start = coverage.requested_start_ms
    end = coverage.requested_end_ms
    pages = coverage.pages
    complete = coverage.complete
    oldest = coverage.oldest_mts_created
    newest = coverage.newest_mts_created
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start > end
        or not isinstance(pages, int)
        or isinstance(pages, bool)
        or pages < 0
        or not isinstance(complete, bool)
        or (complete and pages < 1)
    ):
        return False
    if oldest is not None and (
        not isinstance(oldest, int) or isinstance(oldest, bool)
    ):
        return False
    if newest is not None and (
        not isinstance(newest, int) or isinstance(newest, bool)
    ):
        return False
    return not (
        (oldest is None) != (newest is None)
        or (oldest is not None and newest is not None and oldest > newest)
    )


def match_unknown_attempt(
    attempt: UnknownSubmitAttempt,
    active: Sequence[ActiveFundingOffer],
    history: Sequence[ActiveFundingOffer],
    coverage: FundingOfferHistoryCoverage,
) -> MatchResult:
    """Return exact/zero only when the complete history fence proves it."""
    if (
        not _coverage_invariants_hold(coverage)
        or not coverage.complete
        or coverage.requested_start_ms > attempt.started_at_ms
        or coverage.requested_end_ms < attempt.started_at_ms
    ):
        return MatchResult("incomplete")
    by_id: dict[str, tuple[ActiveFundingOffer, bool]] = {}
    for offer, from_history in (
        *((value, True) for value in history),
        *((value, False) for value in active),
    ):
        prior = by_id.get(offer.venue_offer_id)
        if prior is not None and _immutable_offer_identity(prior[0]) != _immutable_offer_identity(
            offer
        ):
            return MatchResult("incomplete")
        by_id[offer.venue_offer_id] = (offer, from_history)
    candidates: list[ActiveFundingOffer] = []
    for offer, from_history in by_id.values():
        history_fence_covers_offer = not from_history or (
            coverage.oldest_mts_created is not None
            and coverage.newest_mts_created is not None
            and coverage.oldest_mts_created <= offer.mts_created
            and coverage.newest_mts_created >= offer.mts_created
        )
        if history_fence_covers_offer and offer_matches_attempt_identity(
            attempt,
            offer,
            observed_end_ms=coverage.requested_end_ms,
        ):
            candidates.append(offer)
    if not candidates:
        return MatchResult("zero_match")
    if len(candidates) > 1:
        return MatchResult("multiple_match", candidates=tuple(candidates))
    return MatchResult("exact_match", candidates[0], tuple(candidates))


def _immutable_offer_identity(offer: ActiveFundingOffer) -> tuple[object, ...]:
    return (
        offer.symbol,
        offer.amount_original if offer.amount_original is not None else offer.amount,
        offer.rate_observed,
        offer.rate_decimal,
        offer.period_days,
        offer.mts_created,
        offer.offer_type,
        normalized_match_flags(offer.flags) if offer.flags is not None else None,
    )


def attempt_from_row(
    row: Any, *, signal_correlation_id: UUID | None = None
) -> UnknownSubmitAttempt | None:
    """Validate and materialize an immutable attempt projection.

    Invalid or legacy partial rows are deliberately unusable for money-state
    resolution.  The payload hash is rechecked so a forged projection cannot
    change the identity selected during replay.
    """
    payload = row.normalized_payload
    if not isinstance(payload, dict):
        return None
    try:
        if fingerprint_submit_payload(payload) != row.payload_sha256:
            return None
        amount = Decimal(str(payload["amount"]))
        rate = Decimal(str(payload["rate"]))
        period_days = int(payload["period"])
        offer_type = str(payload["type"])
        raw_flags = payload["flags"]
        if not isinstance(raw_flags, (Mapping, int)) or isinstance(raw_flags, bool):
            return None
        if payload.get("symbol") != row.symbol:
            return None
        signal_id = signal_correlation_id or UUID(int=0)
        account_id = str(row.exchange_account_id)
        reference = ReservationRef(
            execution_decision_id=row.execution_decision_id,
            cid=row.cid,
            signal_correlation_id=signal_id,
        )
    except (AttributeError, InvalidOperation, KeyError, TypeError, ValueError):
        return None
    if (
        not amount.is_finite()
        or amount < 0
        or not rate.is_finite()
        or period_days <= 0
        or not offer_type
        or row.started_at_ms < 0
    ):
        return None
    return UnknownSubmitAttempt(
        attempt_id=row.attempt_id,
        execution_decision_id=row.execution_decision_id,
        account_id=account_id,
        symbol=row.symbol,
        cid=row.cid,
        amount=amount,
        rate=rate,
        period_days=period_days,
        offer_type=offer_type,
        flags=raw_flags,
        started_at_ms=row.started_at_ms,
        signal_correlation_id=signal_id,
        reservation_ref=reference,
    )


def _offer_from_mapping(value: object) -> ActiveFundingOffer | None:
    if not isinstance(value, Mapping):
        return None
    try:
        offer_id = value["venue_offer_id"]
        symbol = value["symbol"]
        amount = Decimal(str(value["amount_remaining"]))
        amount_original = Decimal(str(value["amount_original"]))
        rate = Decimal(str(value["rate"]))
        period = value["period_days"]
        mts_created = value["mts_created"]
        offer_type = value["offer_type"]
        flags = value["flags"]
        status = value["status"]
        if (
            not isinstance(offer_id, str)
            or not offer_id
            or not isinstance(symbol, str)
            or not symbol
            or not isinstance(period, int)
            or isinstance(period, bool)
            or not isinstance(mts_created, int)
            or isinstance(mts_created, bool)
            or not isinstance(offer_type, str)
            or not offer_type
            or not isinstance(flags, (Mapping, int))
            or isinstance(flags, bool)
            or not isinstance(status, str)
            or not amount.is_finite()
            or not amount_original.is_finite()
            or not rate.is_finite()
        ):
            return None
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None
    return ActiveFundingOffer(
        venue_offer_id=offer_id,
        symbol=symbol,
        amount=amount,
        rate=float(rate),
        period_days=period,
        mts_created=mts_created,
        status=status,
        amount_original=amount_original,
        mts_updated=(
            value.get("mts_updated") if isinstance(value.get("mts_updated"), int) else None
        ),
        offer_type=offer_type,
        flags=dict(flags) if isinstance(flags, Mapping) else flags,
        rate_observed=True,
        rate_decimal=rate,
    )


def match_attempt_to_snapshot(
    attempt: UnknownSubmitAttempt,
    payload: Mapping[str, Any],
) -> MatchResult:
    """Evaluate an event-log snapshot, failing closed on incomplete metadata."""
    coverage_value = payload.get("coverage")
    if not isinstance(coverage_value, Mapping):
        return MatchResult("incomplete")
    try:
        start = coverage_value["offer_history_start_ms"]
        end = coverage_value["offer_history_end_ms"]
        pages = coverage_value.get("offer_history_pages", 0)
        complete = coverage_value["offer_history_complete"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(pages, int)
            or isinstance(pages, bool)
            or not isinstance(complete, bool)
            or pages < 0
            or (complete and pages < 1)
            or start > end
        ):
            return MatchResult("incomplete")
        oldest = coverage_value.get("offer_history_oldest_mts")
        newest = coverage_value.get("offer_history_newest_mts")
        if oldest is not None and (not isinstance(oldest, int) or isinstance(oldest, bool)):
            return MatchResult("incomplete")
        if newest is not None and (not isinstance(newest, int) or isinstance(newest, bool)):
            return MatchResult("incomplete")
        if (oldest is None) != (newest is None) or (
            oldest is not None and newest is not None and oldest > newest
        ):
            return MatchResult("incomplete")
    except KeyError:
        return MatchResult("incomplete")
    query_started_at_ms = payload.get("query_started_at_ms")
    query_finished_at_ms = payload.get("query_finished_at_ms")
    if (
        not isinstance(query_started_at_ms, int)
        or isinstance(query_started_at_ms, bool)
        or not isinstance(query_finished_at_ms, int)
        or isinstance(query_finished_at_ms, bool)
        or query_finished_at_ms < query_started_at_ms
        or end < query_started_at_ms
    ):
        return MatchResult("incomplete")

    active_values = payload.get("offers")
    history_values = payload.get("offer_history")
    if not isinstance(active_values, list) or not isinstance(history_values, list):
        return MatchResult("incomplete")
    active = [_offer_from_mapping(value) for value in active_values]
    history = [_offer_from_mapping(value) for value in history_values]
    # A same-symbol record with missing exact identity metadata makes both a
    # one-match and zero-match conclusion unsafe.  Unrelated legacy symbols do
    # not weaken this attempt's fence.
    if any(
        parsed is None and isinstance(raw, Mapping) and raw.get("symbol") == attempt.symbol
        for raw, parsed in [
            *zip(active_values, active, strict=True),
            *zip(history_values, history, strict=True),
        ]
    ):
        return MatchResult("incomplete")
    return match_unknown_attempt(
        attempt,
        tuple(value for value in active if value is not None),
        tuple(value for value in history if value is not None),
        FundingOfferHistoryCoverage(
            requested_start_ms=start,
            requested_end_ms=end,
            oldest_mts_created=oldest,
            newest_mts_created=newest,
            pages=pages,
            complete=complete,
        ),
    )


def amount_seen_since_start(attempt: UnknownSubmitAttempt, payload: Mapping[str, Any]) -> bool:
    """Whether any stored offer of the attempt's symbol carries its exact amount
    and was created since it started (or its creation time or amount is unreadable).

    With D3a fingerprints the amount is the submit's identity, so such an offer
    that still fails another identity field is a near miss, not proof of
    absence: an automatic "not sent" must not be derived while one exists.
    Operator adjudication and the projector keep ``zero_match`` as their rule;
    this is the extra condition evidence alone has to meet.
    """
    for key in ("offers", "offer_history"):
        values = payload.get(key)
        for value in values if isinstance(values, list) else ():
            if not isinstance(value, Mapping) or value.get("symbol") != attempt.symbol:
                continue
            created = value.get("mts_created")
            try:
                original = Decimal(str(value.get("amount_original")))
            except ArithmeticError:
                return True  # unreadable same-symbol amount: cannot rule it out
            if original == attempt.amount and (
                    not isinstance(created, int)
                    or created >= attempt.started_at_ms - VENUE_CLOCK_TOLERANCE_MS):
                return True
    return False


def deterministic_resolution_evidence(
    *,
    reconcile_event_seq: int,
    payload: Mapping[str, Any],
    candidate_count: int | None = None,
    venue_offer_id: str | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "reconcile_event_seq": reconcile_event_seq,
        "query_started_at_ms": payload.get("query_started_at_ms"),
        "query_finished_at_ms": payload.get("query_finished_at_ms"),
    }
    if candidate_count is not None:
        evidence["candidate_count"] = candidate_count
    if venue_offer_id is not None:
        evidence["venue_offer_id"] = venue_offer_id
    return evidence


__all__ = [
    "MatchResult",
    "UnknownSubmitAttempt",
    "amount_seen_since_start",
    "attempt_from_row",
    "deterministic_resolution_evidence",
    "match_attempt_to_snapshot",
    "match_unknown_attempt",
    "normalized_match_flags",
    "offer_matches_attempt_identity",
]
