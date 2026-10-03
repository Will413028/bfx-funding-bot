"""The neutral matcher against the legacy one it re-states (S1-3e3).

Layer 1 runs the legacy ``match_attempt_to_snapshot`` + ``amount_seen_since_start`` on a
stored-payload encoding of each case and the neutral ``match_unknown`` on the typed
encoding; the verdicts must be equal unless the case declares the divergence.

Layer 2 compares ``decide_unknown`` with a test-owned frozen copy of the branch
predicates of ``BootRecovery._resolve_unknown``; an AST-identity check pins that copy
to the source it was taken from (a legacy edit fails there first).

Declared divergences (each only where the ledger is deliberately stricter):
``g1_symbol``  the port never fetched the symbol's history (legacy fetched account-wide)
``bind_observed_after``  a bind also needs a query that began after the UNKNOWN
``shared``  two open UNKNOWNs share a candidate
(Also out of this pure layer: the resolver runs only on an accepted observation.)
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from matching_fixtures import (
    ATTEMPT_ID,
    CREATED,
    HISTORY_END,
    HISTORY_START,
    QUERY_FINISHED,
    QUERY_STARTED,
    STARTED,
    UNKNOWN_AT,
    coverage,
    evidence,
    history,
    offer,
    terms,
)

from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.unknown_matching import (
    UnknownSubmitAttempt,
    amount_seen_since_start,
    match_attempt_to_snapshot,
)
from bfx_funding_bot.modules.ledger import (
    UNKNOWN_SETTLE_MS,
    Coverage,
    Offer,
    OfferHistory,
    decide_unknown,
    match_unknown,
)

SETTLED = STARTED + UNKNOWN_SETTLE_MS
COVERED = {"history_oldest_mts_created": CREATED, "history_newest_mts_created": CREATED}


@dataclass(frozen=True)
class Case:
    id: str
    offers: tuple[Offer, ...] = ()
    past: tuple[OfferHistory, ...] = ()
    cov: dict[str, Any] = field(default_factory=dict)
    query_started: int = QUERY_STARTED
    query_finished: int = QUERY_FINISHED
    unknown_at: int = UNKNOWN_AT
    attributed: frozenset[str] = frozenset()
    shared: frozenset[str] = frozenset()
    # (legacy kind, neutral kind) when the matcher verdicts are declared to differ
    kind_divergence: tuple[str, str] | None = None
    # name of the declared decision divergence, with (legacy action, neutral action)
    decision_divergence: tuple[str, str, str] | None = None


def _wire(row: Offer, status: str) -> dict[str, Any]:
    original = row.amount_original
    return {
        "venue_offer_id": row.venue_offer_id, "symbol": row.symbol,
        "amount_remaining": str(row.amount_remaining),
        "amount_original": None if original is None else str(original),
        "rate": None if row.rate is None else str(row.rate), "period_days": row.period_days,
        "mts_created": row.mts_created, "offer_type": row.offer_type, "flags": row.flags,
        "status": status, "mts_updated": row.mts_updated,
    }


def legacy_inputs(case: Case) -> tuple[UnknownSubmitAttempt, dict[str, Any]]:
    cov: Coverage = coverage(**case.cov)
    t = terms()
    signal = UUID("11111111-1111-1111-1111-111111111111")
    attempt = UnknownSubmitAttempt(
        attempt_id=ATTEMPT_ID, execution_decision_id="d", account_id="a", symbol=t.symbol,
        cid=7, amount=t.amount, rate=t.rate, period_days=t.period_days, offer_type=t.offer_type,
        flags=t.flags, started_at_ms=t.started_at_ms, signal_correlation_id=signal,
        reservation_ref=ReservationRef(execution_decision_id="d", cid=7, signal_correlation_id=signal),
    )
    payload = {
        "query_started_at_ms": case.query_started, "query_finished_at_ms": case.query_finished,
        "offers": [_wire(row, row.status) for row in case.offers],
        "offer_history": [_wire(item.offer, item.offer.status) for item in case.past],
        "coverage": {
            "offer_history_start_ms": cov.history_requested_start_ms,
            "offer_history_end_ms": cov.history_requested_end_ms,
            "offer_history_pages": cov.offer_history_pages,
            "offer_history_complete": cov.offer_history_complete,
            "offer_history_oldest_mts": cov.history_oldest_mts_created,
            "offer_history_newest_mts": cov.history_newest_mts_created,
        },
    }
    return attempt, payload


def neutral_inputs(case: Case):
    t = terms(unknown_recorded_at_ms=case.unknown_at)
    ev = evidence(
        case.offers, case.past, query_started_at_ms=case.query_started,
        query_finished_at_ms=case.query_finished, coverage=coverage(**case.cov),
    )
    return t, ev


CASES: tuple[Case, ...] = (
    Case("exact_active", (offer("A"),)),
    Case("exact_partially_filled", (offer("A", amount_remaining=Decimal("30"), status="partially_filled"),)),
    Case("exact_executed_history", past=(history("H", "executed"),), cov=COVERED),
    Case("exact_canceled_by_us_history", past=(history("H", "canceled"),), cov=COVERED),
    Case("zero_empty", ),
    Case("zero_other_rate_is_near_miss", (offer("N", rate=Decimal("0.002")),)),
    Case("zero_other_period", (offer("N", period_days=3),)),
    Case("zero_other_type", (offer("N", offer_type="MARKET"),)),
    Case("zero_other_flags", (offer("N", flags=64),)),
    Case("zero_other_symbol", (offer("N", symbol="fUSD"),)),
    Case("multiple", (offer("A"), offer("B"))),
    Case("rate_wire_precision_equal",
         (offer("A", rate=Decimal("0.00030000000000000004")),)),
    Case("rate_wire_precision_differs",
         (offer("A", rate=Decimal("0.0003")),)),
    Case("stamped_5s_before_start", (offer("A", created=STARTED - 5_000),)),
    Case("stamped_just_over_5s_before_start", (offer("A", created=STARTED - 5_001),)),
    Case("stamped_at_requested_end", (offer("A", created=HISTORY_END),)),
    Case("stamped_after_requested_end", (offer("A", created=HISTORY_END + 1),)),
    Case("older_same_amount_not_near_miss", (offer("O", rate=Decimal("0.002"), created=STARTED - 6_000),)),
    Case("unreadable_same_symbol_rate", (offer("X", rate=None),)),
    Case("unreadable_same_symbol_flags", (offer("X", flags=None),)),
    Case("unreadable_same_symbol_original", (offer("X", amount_original=None),)),
    Case("unreadable_same_symbol_in_history", past=(history("X", offer_type=None),), cov=COVERED),
    Case("unreadable_other_symbol_does_not_taint", (offer("A"), offer("X", symbol="fUSD", rate=None))),
    Case("history_starts_after_attempt", cov={"history_requested_start_ms": STARTED + 1}),
    Case("history_starts_at_attempt", cov={"history_requested_start_ms": STARTED}),
    Case("history_ends_before_attempt", cov={"history_requested_end_ms": STARTED - 1,
                                            "history_requested_start_ms": HISTORY_START}),
    Case("history_incomplete", cov={"offer_history_complete": False}),
    Case("history_complete_zero_pages", cov={"offer_history_pages": 0}),
    Case("history_negative_pages", cov={"offer_history_pages": -1}),
    Case("history_range_half_null", cov={"history_oldest_mts_created": CREATED}),
    Case("history_range_inverted",
         cov={"history_oldest_mts_created": CREATED + 1, "history_newest_mts_created": CREATED}),
    Case("history_end_before_query_start", cov={"history_requested_end_ms": QUERY_STARTED - 1}),
    Case("query_finished_before_started", query_finished=QUERY_STARTED - 1),
    Case("terminal_outside_observed_range", past=(history("H"),),
         cov={"history_oldest_mts_created": CREATED + 5, "history_newest_mts_created": CREATED + 9}),
    Case("terminal_without_observed_range", past=(history("H"),)),
    Case("identity_conflict_active_vs_history", (offer("A"),),
         (history("A", "canceled", rate=Decimal("0.002")),), cov=COVERED),
    Case("same_offer_active_and_history", (offer("A"),), (history("A", "canceled"),), cov=COVERED),
    Case("not_settled", (offer("A"),), query_started=SETTLED - 1,
         query_finished=SETTLED, cov={"history_requested_end_ms": SETTLED - 1}),
    Case("settled_exactly", (offer("A"),), query_started=SETTLED, query_finished=SETTLED + 1,
         cov={"history_requested_end_ms": SETTLED}),
    Case("zero_not_settled", query_started=SETTLED - 1, query_finished=SETTLED,
         cov={"history_requested_end_ms": SETTLED - 1}),
    Case("zero_settled_exactly", query_started=SETTLED, query_finished=SETTLED + 1,
         cov={"history_requested_end_ms": SETTLED}),
    Case("attributed_candidate", (offer("A"),), attributed=frozenset({"A"})),
    Case("unattributed_other_offer", (offer("A"),), attributed=frozenset({"Z"})),
    # --- declared divergences ---------------------------------------------------------
    Case("g1_symbol_never_fetched", cov={"history_symbols": frozenset({"fUSD"})},
         kind_divergence=("zero_match", "incomplete"),
         decision_divergence=("g1_symbol", "not_accepted", "leave_open")),
    Case("g1_symbol_undeclared_exact_still_blocked", (offer("A"),), cov={"history_symbols": None},
         kind_divergence=("exact_match", "incomplete"),
         decision_divergence=("g1_symbol", "bind", "leave_open")),
    Case("opened_in_same_run_zero", unknown_at=QUERY_STARTED),
    Case("opened_in_same_run_exact", (offer("A"),), unknown_at=QUERY_STARTED,
         decision_divergence=("bind_observed_after", "bind", "leave_open")),
    Case("shared_candidate", (offer("A"),), shared=frozenset({"A"}),
         decision_divergence=("shared", "bind", "leave_open")),
)


def _legacy_verdict(case: Case):
    attempt, payload = legacy_inputs(case)
    result = match_attempt_to_snapshot(attempt, payload)
    ids = sorted(offer_.venue_offer_id for offer_ in result.candidates)
    one = result.offer.venue_offer_id if result.offer is not None else None
    return result, (result.kind, one, ids, amount_seen_since_start(attempt, payload)), payload


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_matcher_verdict_equals_legacy_unless_declared(case: Case) -> None:
    legacy, (kind, one, ids, seen), _ = _legacy_verdict(case)
    t, ev = neutral_inputs(case)
    match = match_unknown(t, ev)
    if case.kind_divergence is not None:
        assert (kind, match.kind) == case.kind_divergence
        assert match.incomplete_reason == "history_symbol_undeclared"
        return
    assert (match.kind, match.venue_offer_id, list(match.candidate_venue_offer_ids)) == (kind, one, ids)
    assert match.amount_seen_since_start is seen
    assert legacy.kind == match.kind


# --- layer 2: the decision layer ------------------------------------------------------------


def frozen_legacy_decision(
    *, kind: str, query_started: int, started: int, settle: int, history_end: Any,
    amount_seen: bool, observed_after: bool, attributed: bool,
) -> str:
    """Branch predicates of ``BootRecovery._resolve_unknown`` (boot_recovery.py, main b86cfdbd).

    Their source text is pinned by ``FROZEN_PREDICATES`` below.
    """
    settled_at = started + settle
    if query_started < settled_at:
        return "leave_open"
    if kind == "exact_match":
        return "leave_open" if attributed else "bind"
    if (kind == "zero_match" and isinstance(history_end, int) and history_end >= settled_at
            and not amount_seen and observed_after):
        return "not_accepted"
    return "leave_open"


_ACTION = {"bind": "bound_to_venue", "not_accepted": "not_accepted", "leave_open": "leave_open"}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_decision_equals_the_frozen_legacy_branches_unless_declared(case: Case) -> None:
    _, (kind, one, _, seen), payload = _legacy_verdict(case)
    t, ev = neutral_inputs(case)
    expected = frozen_legacy_decision(
        kind=kind, query_started=case.query_started, started=STARTED, settle=UNKNOWN_SETTLE_MS,
        history_end=payload["coverage"]["offer_history_end_ms"], amount_seen=seen,
        observed_after=case.query_started > case.unknown_at,
        attributed=one in case.attributed,
    )
    decision = decide_unknown(
        t, ev, match_unknown(t, ev), settle_ms=UNKNOWN_SETTLE_MS, attributed=case.attributed,
        shared=case.shared,
    )
    if case.decision_divergence is not None:
        _, legacy_action, neutral_action = case.decision_divergence
        assert expected == legacy_action
        assert decision.action == neutral_action
        return
    assert decision.action == _ACTION[expected]


def test_every_declared_divergence_is_exercised_and_none_is_silent() -> None:
    names = {c.decision_divergence[0] for c in CASES if c.decision_divergence}
    assert names == {"g1_symbol", "bind_observed_after", "shared"}


# --- the frozen branches still are the legacy source -------------------------------------------

# The conditions, verbatim from the legacy method, in the order they appear in its loop.
FROZEN_PREDICATES = {
    "settle": "query_started_at_ms < settled_at",
    "bind": 'match.kind == "exact_match" and match.offer is not None',
    "attributed": "await self._offer_attributed(session, UUID(attempt.account_id), offer)",
    "zero": (
        'match.kind == "zero_match" and isinstance(history_end, int) '
        "and history_end >= settled_at and not amount_seen_since_start(attempt, payload) "
        "and await self._observed_after_opening(session, uncertainty, snapshot_seq, "
        "query_started_at_ms)"
    ),
}


def _dump(expression: str) -> str:
    wrapped = ast.parse(f"async def _f():\n    return {expression}")
    return ast.dump(wrapped.body[0].body[0].value)  # type: ignore[attr-defined]


def _legacy_loop() -> ast.For:
    source = textwrap.dedent(inspect.getsource(BootRecovery._resolve_unknown))
    function = ast.parse(source).body[0]
    loops = [n for n in ast.walk(function) if isinstance(n, ast.For)
             and isinstance(n.target, ast.Name) and n.target.id == "attempt"
             and any(isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name)
                     and s.targets[0].id == "match" for s in n.body)]
    assert len(loops) == 1
    return loops[0]


def test_frozen_predicates_are_ast_identical_to_the_legacy_source() -> None:
    loop = _legacy_loop()
    ifs = [s for s in loop.body if isinstance(s, ast.If)]
    settle = next(s for s in ifs if isinstance(s.test, ast.Compare))
    chain = next(s for s in ifs if isinstance(s.test, ast.BoolOp))
    zero = chain.orelse[0]
    assert isinstance(zero, ast.If)
    attributed = next(
        n for n in ast.walk(chain) if isinstance(n, ast.If) and isinstance(n.test, ast.Await)
    )
    assert ast.dump(settle.test) == _dump(FROZEN_PREDICATES["settle"])
    assert ast.dump(chain.test) == _dump(FROZEN_PREDICATES["bind"])
    assert ast.dump(attributed.test) == _dump(FROZEN_PREDICATES["attributed"])
    assert ast.dump(zero.test) == _dump(FROZEN_PREDICATES["zero"])
    # And the `settled_at` they refer to.
    assigns = [s for s in loop.body if isinstance(s, ast.Assign) and s.targets[0].id == "settled_at"]  # type: ignore[attr-defined]
    assert ast.dump(assigns[0].value) == _dump("attempt.started_at_ms + self._unknown_settle_ms")


def test_the_pin_catches_an_edited_predicate() -> None:
    assert _dump("query_started_at_ms <= settled_at") != _dump(FROZEN_PREDICATES["settle"])

