"""Neutral, fail-closed matching of an UNKNOWN submit against one stored observation.

Pure: no storage, no clock. The attempt's own terms and the persisted evidence
of one observation are the only inputs. ``match_unknown`` is the shared rule an
operator verdict and the system resolver both start from; ``decide_unknown`` is
what only the system adds (settle, observed-after, attribution, near miss).

The candidate rule, the 5 s lower tolerance (none above), the incomplete
conditions, the near-miss guard and the settle window follow the legacy boot
recovery rule, re-stated here (the legacy matcher and the differential test that
pinned the two together were retired; both remain at 5097d3c7). Stricter than
legacy, on purpose: the history symbol must be declared by the port, an offer
shared by two open UNKNOWNs is left to an operator, and a bind also needs an
observation that began after the UNKNOWN was recorded.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from bfx_funding_bot.core.venue_time import VENUE_CLOCK_TOLERANCE_MS

if TYPE_CHECKING:
    from bfx_funding_bot.modules.ledger import Coverage, JsonObject, Offer, OfferHistory

# How long after a submit started its offer may still be absent from the venue's answers.
UNKNOWN_SETTLE_MS = 120_000

type UnknownMatchKind = Literal["exact_match", "zero_match", "multiple_match", "incomplete"]
type AutoAction = Literal["bound_to_venue", "not_accepted", "leave_open"]


@dataclass(frozen=True, slots=True)
class UnknownTerms:
    """The UNKNOWN attempt as journaled: what was sent, when, and when it became UNKNOWN."""

    attempt_id: UUID
    symbol: str
    amount: Decimal
    rate: Decimal
    period_days: int
    offer_type: str
    flags: JsonObject | int
    started_at_ms: int
    unknown_recorded_at_ms: int


@dataclass(frozen=True, slots=True)
class MatchEvidence:
    """One stored observation, read back (never the in-memory object that was accepted)."""

    observation_id: UUID
    query_started_at_ms: int
    query_finished_at_ms: int
    coverage: Coverage
    offers: tuple[Offer, ...]
    offer_history: tuple[OfferHistory, ...]


@dataclass(frozen=True, slots=True)
class UnknownMatch:
    kind: UnknownMatchKind
    venue_offer_id: str | None  # the one candidate (exact_match only)
    venue_status: str | None  # active / partially_filled, or the terminal kind (executed / canceled)
    candidate_venue_offer_ids: tuple[str, ...]  # sorted, complete; callers cap what they store
    amount_seen_since_start: bool  # near-miss guard: the fingerprint amount exists, unmatched
    incomplete_reason: str | None


@dataclass(frozen=True, slots=True)
class AutoDecision:
    """What the system does with one UNKNOWN; every leave_open carries its reason.

    leave_open reasons: not_settled, not_observed_after_unknown, incomplete,
    multiple_match, candidate_attributed, candidate_shared, history_before_settle,
    near_miss.
    """

    action: AutoAction
    venue_offer_id: str | None
    reason: str


def _flags(value: JsonObject | int) -> Mapping[str, object]:
    return dict(value) if isinstance(value, Mapping) else {"raw": value}


def _original(offer: Offer) -> Decimal:
    return offer.amount_original if offer.amount_original is not None else offer.amount_remaining


def _readable(offer: Offer) -> bool:
    """Exact-identity metadata present (legacy: the stored row parses)."""
    return (
        offer.amount_original is not None
        and offer.rate is not None
        and offer.period_days is not None
        and bool(offer.offer_type)
        and offer.flags is not None
        and not isinstance(offer.flags, bool)
    )


def _identity(offer: Offer) -> tuple[object, ...]:
    return (
        offer.symbol, _original(offer), offer.rate_observed, offer.rate, offer.period_days,
        offer.mts_created, offer.offer_type,
        _flags(offer.flags) if offer.flags is not None else None,
    )


def _is_candidate(terms: UnknownTerms, offer: Offer, observed_end_ms: int) -> bool:
    return (
        offer.symbol == terms.symbol
        and _original(offer) == terms.amount
        and offer.rate_observed
        and offer.rate is not None
        and offer.rate == terms.rate
        and offer.period_days == terms.period_days
        and terms.started_at_ms - VENUE_CLOCK_TOLERANCE_MS <= offer.mts_created <= observed_end_ms
        and offer.offer_type is not None
        and offer.offer_type == terms.offer_type
        and offer.flags is not None
        and _flags(offer.flags) == _flags(terms.flags)
    )


def _coverage_invalid(coverage: Coverage) -> bool:
    start, end = coverage.history_requested_start_ms, coverage.history_requested_end_ms
    oldest, newest = coverage.history_oldest_mts_created, coverage.history_newest_mts_created
    return (
        start is None
        or end is None
        or start > end
        or coverage.offer_history_pages < 0
        or (coverage.offer_history_complete and coverage.offer_history_pages < 1)
        or (oldest is None) != (newest is None)
        or (oldest is not None and newest is not None and oldest > newest)
    )


def _incomplete(reason: str, seen: bool) -> UnknownMatch:
    return UnknownMatch("incomplete", None, None, (), seen, reason)


def amount_seen_since_start(terms: UnknownTerms, evidence: MatchEvidence) -> bool:
    """Any same-symbol stored offer, active or terminal, carrying the fingerprint amount
    and created since the attempt started (or whose amount is unreadable).

    Such an offer that fails another identity field is a near miss, not proof of absence.
    """
    for offer in (*evidence.offers, *(item.offer for item in evidence.offer_history)):
        if offer.symbol != terms.symbol:
            continue
        if offer.amount_original is None:
            return True  # unreadable same-symbol amount: cannot rule it out
        if (
            offer.amount_original == terms.amount
            and offer.mts_created >= terms.started_at_ms - VENUE_CLOCK_TOLERANCE_MS
        ):
            return True
    return False


def match_unknown(terms: UnknownTerms, evidence: MatchEvidence) -> UnknownMatch:
    """exact / zero only when the stored complete history fence proves it."""
    coverage = evidence.coverage
    seen = amount_seen_since_start(terms, evidence)
    if _coverage_invalid(coverage):
        return _incomplete("coverage_invalid", seen)
    start, end = coverage.history_requested_start_ms, coverage.history_requested_end_ms
    assert start is not None and end is not None
    if evidence.query_finished_at_ms < evidence.query_started_at_ms or end < evidence.query_started_at_ms:
        return _incomplete("query_range_invalid", seen)
    # The port fetches history per symbol; a symbol it never fetched has zero rows
    # because nothing was asked, which proves nothing.
    if coverage.history_symbols is None or terms.symbol not in coverage.history_symbols:
        return _incomplete("history_symbol_undeclared", seen)
    rows = (*evidence.offers, *(item.offer for item in evidence.offer_history))
    if any(not _readable(offer) for offer in rows if offer.symbol == terms.symbol):
        return _incomplete("unreadable_offer", seen)
    if (
        not coverage.offer_history_complete
        or start > terms.started_at_ms
        or end < terms.started_at_ms
    ):
        return _incomplete("history_window", seen)
    # Unreadable rows of other symbols are dropped, as legacy drops them.
    by_id: dict[str, tuple[Offer, bool, str]] = {}
    ordered: tuple[tuple[Offer, bool, str], ...] = (
        *((item.offer, True, item.terminal_kind) for item in evidence.offer_history),
        *((offer, False, offer.status) for offer in evidence.offers),
    )
    for offer, from_history, status in ordered:
        if not _readable(offer):
            continue
        prior = by_id.get(offer.venue_offer_id)
        if prior is not None and _identity(prior[0]) != _identity(offer):
            return _incomplete("identity_conflict", seen)
        by_id[offer.venue_offer_id] = (offer, from_history, status)
    oldest, newest = coverage.history_oldest_mts_created, coverage.history_newest_mts_created
    found: list[tuple[Offer, str]] = []
    for offer, from_history, status in by_id.values():
        covered = not from_history or (
            oldest is not None
            and newest is not None
            and oldest <= offer.mts_created
            and newest >= offer.mts_created
        )
        if covered and _is_candidate(terms, offer, end):
            found.append((offer, status))
    ids = tuple(sorted(offer.venue_offer_id for offer, _ in found))
    if not found:
        return UnknownMatch("zero_match", None, None, (), seen, None)
    if len(found) > 1:
        return UnknownMatch("multiple_match", None, None, ids, seen, None)
    offer, status = found[0]
    return UnknownMatch("exact_match", offer.venue_offer_id, status, ids, seen, None)


def decide_unknown(
    terms: UnknownTerms,
    evidence: MatchEvidence,
    match: UnknownMatch,
    *,
    settle_ms: int,
    attributed: Collection[str],
    shared: Collection[str] = (),
) -> AutoDecision:
    """The system's verdict; anything not provably safe stays open for an operator.

    ``attributed``: venue offers some other attempt or quarantine already owns.
    ``shared``: venue offers that two or more open UNKNOWNs both match.
    """
    started = evidence.query_started_at_ms
    if started < terms.started_at_ms + settle_ms:
        return AutoDecision("leave_open", None, "not_settled")
    if started <= terms.unknown_recorded_at_ms:
        return AutoDecision("leave_open", None, "not_observed_after_unknown")
    if match.kind == "incomplete":
        return AutoDecision("leave_open", None, "incomplete")
    if match.kind == "multiple_match":
        return AutoDecision("leave_open", None, "multiple_match")
    if match.kind == "exact_match":
        offer_id = match.venue_offer_id
        assert offer_id is not None
        if offer_id in attributed:
            return AutoDecision("leave_open", offer_id, "candidate_attributed")
        if offer_id in shared:
            return AutoDecision("leave_open", offer_id, "candidate_shared")
        return AutoDecision("bound_to_venue", offer_id, "exact_fingerprint_match")
    end = evidence.coverage.history_requested_end_ms
    if end is None or end < terms.started_at_ms + settle_ms:
        return AutoDecision("leave_open", None, "history_before_settle")
    if match.amount_seen_since_start:
        return AutoDecision("leave_open", None, "near_miss")
    return AutoDecision("not_accepted", None, "fingerprint_absent_from_complete_history")


__all__ = [
    "UNKNOWN_SETTLE_MS",
    "AutoAction",
    "AutoDecision",
    "MatchEvidence",
    "UnknownMatch",
    "UnknownMatchKind",
    "UnknownTerms",
    "amount_seen_since_start",
    "decide_unknown",
    "match_unknown",
]
