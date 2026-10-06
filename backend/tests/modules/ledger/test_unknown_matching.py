"""The neutral UNKNOWN matcher and the system decision layer (pure).

Mutation map (S1-3e3 pre-flight §F): each test names the mutation it kills.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from matching_fixtures import (
    CREATED,
    HISTORY_END,
    HISTORY_START,
    QUERY_STARTED,
    STARTED,
    UNKNOWN_AT,
    coverage,
    evidence,
    history,
    offer,
    terms,
)

from bfx_funding_bot.modules.ledger import (
    UNKNOWN_SETTLE_MS,
    UnknownMatch,
    decide_unknown,
    match_unknown,
)

SETTLE = UNKNOWN_SETTLE_MS


def decide(*, match=None, ev=None, t=None, attributed=(), shared=(), settle=SETTLE):
    t = t or terms()
    ev = ev or evidence((offer(),))
    return decide_unknown(
        t, ev, match or match_unknown(t, ev), settle_ms=settle, attributed=frozenset(attributed),
        shared=frozenset(shared),
    )


def test_settle_window_is_two_minutes() -> None:
    assert UNKNOWN_SETTLE_MS == 120_000


# --- candidate rule ---------------------------------------------------------------


def test_exact_match_on_an_active_offer() -> None:
    match = match_unknown(terms(), evidence((offer("A"),)))
    assert (match.kind, match.venue_offer_id, match.venue_status) == ("exact_match", "A", "active")
    assert match.candidate_venue_offer_ids == ("A",) and not match.incomplete_reason


@pytest.mark.parametrize("kind", ["executed", "canceled"])
def test_exact_match_on_a_terminal_history_offer_reports_the_terminal_kind(kind) -> None:
    ev = evidence(past=(history("H", kind),), coverage=coverage(
        history_oldest_mts_created=CREATED, history_newest_mts_created=CREATED,
    ))
    match = match_unknown(terms(), ev)
    assert (match.kind, match.venue_offer_id, match.venue_status) == ("exact_match", "H", kind)


def test_partially_filled_active_offer_matches_on_amount_original() -> None:
    row = offer("P", amount_remaining=Decimal("40"), status="partially_filled")
    match = match_unknown(terms(), evidence((row,)))
    assert (match.kind, match.venue_status) == ("exact_match", "partially_filled")


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_original": Decimal("100.0001")},
        {"rate": Decimal("0.0011")},
        {"period_days": 3},
        {"symbol": "fUSD"},
        {"rate_observed": False},
    ],
)
def test_any_identity_difference_is_zero_match(changes) -> None:
    assert match_unknown(terms(), evidence((offer(**changes),))).kind == "zero_match"


@pytest.mark.parametrize("changes", [{"offer_type": "MARKET"}, {"flags": 1}, {"flags": {"hidden": 1}}])
def test_offer_type_and_flags_are_compared(changes) -> None:
    """Mutation 10: dropping the offer_type or flags comparison."""
    assert match_unknown(terms(), evidence((offer(**changes),))).kind == "zero_match"


def test_flags_normalise_like_legacy() -> None:
    mapping = {"hidden": True}
    assert match_unknown(terms(flags=mapping), evidence((offer(flags=dict(mapping)),))).kind == (
        "exact_match"
    )
    assert match_unknown(terms(flags=0), evidence((offer(flags={"raw": 0}),))).kind == "exact_match"


def test_rate_compares_full_decimal_precision() -> None:
    wire = Decimal("0.00030000000000000004")
    assert match_unknown(terms(rate=wire), evidence((offer(rate=wire),))).kind == "exact_match"
    assert match_unknown(terms(rate=wire), evidence((offer(rate=Decimal("0.0003")),))).kind == (
        "zero_match"
    )


def test_missing_original_amount_falls_back_to_remaining() -> None:
    row = offer(amount_original=None, amount_remaining=Decimal("100"))
    # Unreadable for the same symbol: the safe answer is incomplete, never a guess.
    assert match_unknown(terms(), evidence((row,))).kind == "incomplete"


# --- venue clock tolerance (mutation 6) ---------------------------------------------


@pytest.mark.parametrize(
    ("created", "kind"),
    [
        (STARTED - 5_000, "exact_match"),  # the venue stamps whole seconds on its own clock
        (STARTED - 5_001, "zero_match"),
        (STARTED, "exact_match"),
        (HISTORY_END, "exact_match"),  # no tolerance above the requested end
        (HISTORY_END + 1, "zero_match"),
    ],
)
def test_created_window_has_five_seconds_below_and_none_above(created, kind) -> None:
    assert match_unknown(terms(), evidence((offer(created=created),))).kind == kind


# --- history fence (mutations 5, 13) ------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"history_requested_start_ms": STARTED + 1},  # mutation 5: starts after the submit
        {"history_requested_end_ms": STARTED - 1},
        {"history_requested_start_ms": None},
        {"history_requested_end_ms": None},
        {"history_requested_start_ms": HISTORY_END + 1},
        {"offer_history_complete": False},
        {"offer_history_pages": 0},
        {"offer_history_pages": -1},
        {"history_oldest_mts_created": CREATED},
        {"history_newest_mts_created": CREATED},
        {"history_oldest_mts_created": CREATED + 1, "history_newest_mts_created": CREATED},
    ],
)
def test_incomplete_or_late_history_proves_nothing(changes) -> None:
    ev = evidence(coverage=coverage(**changes))
    assert match_unknown(terms(), ev).kind == "incomplete"


def test_history_starting_exactly_at_the_submit_still_proves_absence() -> None:
    ev = evidence(coverage=coverage(history_requested_start_ms=STARTED))
    assert match_unknown(terms(), ev).kind == "zero_match"


def test_history_ending_before_the_query_began_is_incomplete() -> None:
    ev = evidence(coverage=coverage(history_requested_end_ms=QUERY_STARTED - 1))
    assert match_unknown(terms(), ev).incomplete_reason == "query_range_invalid"


def test_query_finishing_before_it_started_is_incomplete() -> None:
    assert match_unknown(terms(), evidence(query_finished_at_ms=QUERY_STARTED - 1)).kind == (
        "incomplete"
    )


@pytest.mark.parametrize("symbols", [None, frozenset(), frozenset({"fUSD"})])
def test_g1_a_symbol_the_port_never_fetched_is_incomplete_not_zero(symbols) -> None:
    """Mutation 13 / G1: complete history with zero rows for an unfetched symbol is no proof."""
    match = match_unknown(terms(), evidence(coverage=coverage(history_symbols=symbols)))
    assert (match.kind, match.incomplete_reason) == ("incomplete", "history_symbol_undeclared")


def test_g1_declared_symbols_prove_absence() -> None:
    ev = evidence(coverage=coverage(history_symbols=frozenset({"fUST", "fUSD"})))
    assert match_unknown(terms(), ev).kind == "zero_match"


def test_terminal_offer_outside_the_observed_created_range_is_not_a_candidate() -> None:
    ev = evidence(past=(history("H"),), coverage=coverage(
        history_oldest_mts_created=CREATED + 10, history_newest_mts_created=CREATED + 20,
    ))
    assert match_unknown(terms(), ev).kind == "zero_match"


def test_terminal_offer_without_any_observed_range_is_not_a_candidate() -> None:
    assert match_unknown(terms(), evidence(past=(history("H"),))).kind == "zero_match"


# --- unreadable rows (mutation 8) -----------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_original": None},
        {"rate": None},
        {"period_days": None},
        {"offer_type": None},
        {"offer_type": ""},
        {"flags": None},
        {"flags": True},
    ],
)
@pytest.mark.parametrize("where", ["active", "history"])
def test_unreadable_same_symbol_row_is_incomplete(changes, where) -> None:
    ev = (
        evidence((offer("X", **changes),))
        if where == "active"
        else evidence(past=(history("X", **changes),))
    )
    assert match_unknown(terms(), ev).incomplete_reason == "unreadable_offer"


def test_unreadable_row_of_another_symbol_does_not_taint() -> None:
    ev = evidence((offer("A"), offer("X", symbol="fUSD", rate=None)))
    assert match_unknown(terms(), ev).kind == "exact_match"


# --- identity conflict, duplicates, multiples (mutation 9) ----------------------------------


def test_same_id_with_a_different_immutable_identity_is_incomplete() -> None:
    ev = evidence(
        (offer("A"),), (history("A", "canceled", rate=Decimal("0.002")),),
        coverage=coverage(history_oldest_mts_created=CREATED, history_newest_mts_created=CREATED),
    )
    assert match_unknown(terms(), ev).incomplete_reason == "identity_conflict"


def test_the_same_offer_in_active_and_history_is_one_candidate_active_wins() -> None:
    ev = evidence(
        (offer("A"),), (history("A", "canceled"),),
        coverage=coverage(history_oldest_mts_created=CREATED, history_newest_mts_created=CREATED),
    )
    match = match_unknown(terms(), ev)
    assert (match.kind, match.venue_status) == ("exact_match", "active")


def test_duplicate_history_rows_of_one_id_are_one_candidate() -> None:
    ev = evidence(past=(history("H", "canceled"), history("H", "canceled")), coverage=coverage(
        history_oldest_mts_created=CREATED, history_newest_mts_created=CREATED,
    ))
    assert match_unknown(terms(), ev).kind == "exact_match"


def test_two_candidates_are_multiple_and_bind_neither() -> None:
    match = match_unknown(terms(), evidence((offer("B"), offer("A"))))
    assert (match.kind, match.venue_offer_id, match.venue_status) == ("multiple_match", None, None)
    assert match.candidate_venue_offer_ids == ("A", "B")  # sorted
    decision = decide(match=match, ev=evidence((offer("B"), offer("A"))))
    assert (decision.action, decision.venue_offer_id, decision.reason) == (
        "leave_open", None, "multiple_match",
    )


# --- near miss (mutation 2) ------------------------------------------------------------------


def test_amount_seen_since_start_flags_a_near_miss() -> None:
    ev = evidence((offer("N", rate=Decimal("0.002")),))
    match = match_unknown(terms(), ev)
    assert (match.kind, match.amount_seen_since_start) == ("zero_match", True)
    assert decide(match=match, ev=ev).reason == "near_miss"


@pytest.mark.parametrize(
    ("created", "seen"), [(STARTED - 5_000, True), (STARTED - 5_001, False)]
)
def test_an_older_same_amount_offer_is_not_a_near_miss(created, seen) -> None:
    ev = evidence((offer("O", rate=Decimal("0.002"), created=created),))
    assert match_unknown(terms(), ev).amount_seen_since_start is seen


def test_near_miss_also_looks_into_history_and_other_periods() -> None:
    ev = evidence(past=(history("H", period_days=9),), coverage=coverage(
        history_oldest_mts_created=CREATED, history_newest_mts_created=CREATED,
    ))
    assert match_unknown(terms(), ev).amount_seen_since_start


def test_unreadable_same_symbol_amount_cannot_rule_the_near_miss_out() -> None:
    assert match_unknown(terms(), evidence((offer("U", amount_original=None),))).amount_seen_since_start


def test_other_symbols_and_other_amounts_are_not_near_misses() -> None:
    ev = evidence((offer("S", symbol="fUSD"), offer("T", amount_original=Decimal("101"))))
    assert not match_unknown(terms(), ev).amount_seen_since_start


def test_absence_with_nothing_similar_is_a_clean_zero_match() -> None:
    match = match_unknown(terms(), evidence())
    assert (match.kind, match.amount_seen_since_start, match.candidate_venue_offer_ids) == (
        "zero_match", False, (),
    )


# --- decision layer -----------------------------------------------------------------------


def test_exact_unattributed_settled_observed_after_binds() -> None:
    decision = decide()
    assert (decision.action, decision.venue_offer_id, decision.reason) == (
        "bound_to_venue", "o-1", "exact_fingerprint_match",
    )


def test_clean_absence_is_not_accepted() -> None:
    decision = decide(ev=evidence())
    assert (decision.action, decision.reason) == (
        "not_accepted", "fingerprint_absent_from_complete_history",
    )


@pytest.mark.parametrize("ev_offers", [(offer(),), ()], ids=["bind", "not_accepted"])
def test_settle_boundary_is_inclusive_of_the_settled_instant(ev_offers) -> None:
    """Mutation 1: ``<`` -> ``<=`` (or dropped) on the settle comparison."""
    settled = STARTED + SETTLE
    at = decide(ev=evidence(ev_offers, query_started_at_ms=settled, query_finished_at_ms=settled + 1,
                            coverage=coverage(history_requested_end_ms=settled)))
    before = decide(ev=evidence(ev_offers, query_started_at_ms=settled - 1,
                                query_finished_at_ms=settled,
                                coverage=coverage(history_requested_end_ms=settled)))
    assert at.action != "leave_open" and before.reason == "not_settled"


@pytest.mark.parametrize("ev_offers", [(offer(),), ()], ids=["bind", "not_accepted"])
def test_the_observation_must_begin_strictly_after_the_unknown(ev_offers) -> None:
    """Mutation 7: ``>`` -> ``>=`` or judged against the attempt start instead."""
    later = UNKNOWN_AT + 200_000
    t = terms(unknown_recorded_at_ms=later)
    at = decide(t=t, ev=evidence(ev_offers, query_started_at_ms=later,
                                 query_finished_at_ms=later + 1,
                                 coverage=coverage(history_requested_end_ms=later)))
    after = decide(t=t, ev=evidence(ev_offers, query_started_at_ms=later + 1,
                                    query_finished_at_ms=later + 2,
                                    coverage=coverage(history_requested_end_ms=later + 1)))
    assert at.reason == "not_observed_after_unknown" and after.action != "leave_open"


def test_observed_after_uses_the_unknown_not_the_attempt_start() -> None:
    # Settled (>= start + 120 s) but the UNKNOWN was recorded after the query began.
    t = terms(unknown_recorded_at_ms=QUERY_STARTED + 10)
    assert decide(t=t).reason == "not_observed_after_unknown"


def test_incomplete_leaves_open() -> None:
    ev = evidence(coverage=coverage(offer_history_complete=False))
    assert decide(ev=ev).reason == "incomplete"


def test_attributed_candidate_is_not_bound() -> None:
    """Mutation 3: the attribution check removed."""
    decision = decide(attributed={"o-1"})
    assert (decision.action, decision.reason) == ("leave_open", "candidate_attributed")
    assert decide(attributed={"other"}).action == "bound_to_venue"


def test_shared_candidate_is_not_bound() -> None:
    """Mutation 17: two open UNKNOWNs sharing a candidate both stay open."""
    decision = decide(shared={"o-1"})
    assert (decision.action, decision.reason) == ("leave_open", "candidate_shared")


def test_absence_needs_the_history_to_reach_the_settled_instant() -> None:
    """Mutation 16, at the pure layer: ``match`` and ``evidence`` are caller-supplied here."""
    settled = STARTED + SETTLE
    ev = evidence(coverage=coverage(history_requested_end_ms=settled - 1))
    zero = UnknownMatch("zero_match", None, None, (), False, None)
    assert decide(match=zero, ev=ev).reason == "history_before_settle"
    ev = evidence(coverage=coverage(history_requested_end_ms=settled))
    assert decide(match=zero, ev=ev).action == "not_accepted"


def test_absence_with_a_missing_history_end_stays_open() -> None:
    zero = UnknownMatch("zero_match", None, None, (), False, None)
    ev = evidence(coverage=coverage(history_requested_end_ms=None))
    assert decide(match=zero, ev=ev).reason == "history_before_settle"


def test_a_custom_settle_window_is_honoured() -> None:
    assert decide(settle=QUERY_STARTED - STARTED + 1).reason == "not_settled"
    assert decide(settle=QUERY_STARTED - STARTED).action == "bound_to_venue"


def test_history_start_constant_sanity() -> None:
    assert HISTORY_START < STARTED < UNKNOWN_AT < QUERY_STARTED < HISTORY_END + 1
    assert replace(terms(), symbol="fUSD").symbol == "fUSD"
