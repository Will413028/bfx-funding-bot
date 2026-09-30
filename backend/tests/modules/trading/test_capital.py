"""Synthetic acceptance facts: tests of the fold, not of venue classification."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from typing import get_args
from uuid import UUID

import pytest

from bfx_funding_bot.modules.trading import (
    AcceptedCapitalBasis,
    AppliedPolicy,
    AttemptFact,
    AttemptOutcome,
    Available,
    Blocked,
    CapitalBudget,
    CapitalPolicy,
    CapitalReadContext,
    CapitalResult,
    CapitalScope,
    CapitalSnapshot,
    ComparisonKind,
    ComparisonStatus,
    DifferenceClassification,
    SymbolCapital,
    UncertaintyFact,
    derive_capital,
    evaluate_capital,
)

D = Decimal
ACCOUNT = UUID(int=1)
SCOPE = CapitalScope(ACCOUNT, "test", "fUSD", "alpha")
VALUES = SymbolCapital("fUSD", D("1000"), D("200"), D("300"), D("50"), D("90"),
                       (("alpha", D("350")), ("beta", D("150"))))
BASIS = AcceptedCapitalBasis(
    ACCOUNT, "test", 20, 10, UUID(int=2), 1000, 1100, (VALUES,),
    frozenset(), frozenset(), (), True, None,
)
POLICY = AppliedPolicy(ACCOUNT, "test", "fUSD", 7, "policy-digest", UUID(int=3),
                       CapitalPolicy(True, reserve_amount=D("100"), max_cell_fraction=D("0.5")))
CONTEXT = CapitalReadContext(1200, 500, 30, BASIS.query_id, BASIS.snapshot_seq, None, None)


def attempt(number: int, amount: str, outcome: AttemptOutcome = "pending", *,
            cell: str = "alpha", symbol: str = "fUSD", seq: int = 21) -> AttemptFact:
    return AttemptFact(UUID(int=100 + number), replace(SCOPE, cell_id=cell, symbol=symbol),
                       seq, D(amount), outcome)


def uncertainty(*, symbol: str = "fUSD", is_open: bool = True) -> UncertaintyFact:
    return UncertaintyFact(UUID(int=500), ACCOUNT, "test", symbol, is_open)


def fold(*, scope: CapitalScope = SCOPE, basis: AcceptedCapitalBasis = BASIS,
         attempts: tuple[AttemptFact, ...] = (),
         uncertainties: tuple[UncertaintyFact, ...] = (), policy: AppliedPolicy = POLICY,
         context: CapitalReadContext = CONTEXT) -> CapitalResult:
    return derive_capital(scope=scope, accepted=basis, attempts=attempts,
                          uncertainties=uncertainties, policy=policy, read_context=context)


def test_post_fence_commitments_and_multicell_headroom() -> None:
    facts = (
        attempt(1, "101.0001"), attempt(2, "202.0002", "acknowledged", cell="beta"),
        attempt(3, "900", "rejected"), attempt(4, "800", "not_sent"),
        attempt(5, "999", symbol="fUST"),
    )
    result = fold(attempts=facts)
    assert isinstance(result, Available)
    assert result.view.snapshot == CapitalSnapshot(D("1000"), D("303.0003"), D("1500"),
                                                   D("451.0001"))
    assert result.view.budget == CapitalBudget(D("596.9997"), D("700"), D("248.9999"),
                                               D("248.9999"), None)
    beta = fold(scope=replace(SCOPE, cell_id="beta"), attempts=facts)
    assert isinstance(beta, Available)
    assert beta.view.snapshot.cell_exposure == D("352.0002")
    assert beta.view.budget.max_new_offer == D("347.9998")
    assert beta.view.budget.spendable == result.view.budget.spendable
    assert result.view.applied is POLICY
    assert result.view.snapshot_seq == BASIS.snapshot_seq
    assert result.view.attribution is BASIS


@pytest.mark.parametrize("outcome", ["pending", "acknowledged", "rejected", "not_sent"])
def test_each_outcome_commitment(outcome: AttemptOutcome) -> None:
    result = fold(attempts=(attempt(1, "100", outcome),))
    assert isinstance(result, Available)
    expected = D("100") if outcome in {"pending", "acknowledged"} else D("0")
    assert result.view.snapshot.unreflected_commitments == expected
    assert result.view.snapshot.cell_exposure == D("350") + expected
    assert result.view.snapshot.total_capital == D("1500")


def test_unknown_attempt_is_blocked_even_without_uncertainty_projection() -> None:
    result = fold(attempts=(attempt(1, "400", "unknown"),))
    assert result == Blocked("execution_unknown", (("attempt", str(UUID(int=101))),))


@pytest.mark.parametrize("source", ["current", "accepted", "tail"])
def test_unknown_blocks_its_symbol_only(source: str) -> None:
    basis = BASIS
    facts: tuple[AttemptFact, ...] = ()
    uncertainties: tuple[UncertaintyFact, ...] = ()
    if source == "current":
        uncertainties = (uncertainty(symbol="fUST"),)
    elif source == "accepted":
        basis = replace(BASIS, unresolved_attempts=((UUID(int=101), "fUST"),))
    else:
        facts = (attempt(1, "999", "unknown", symbol="fUST"),)
    result = fold(basis=basis, attempts=facts, uncertainties=uncertainties)
    assert isinstance(result, Available)
    assert result.view.snapshot.unreflected_commitments == D("0")
    assert result.view.budget.max_new_offer == D("350")


def test_open_legacy_uncertainty_without_attempt_blocks() -> None:
    result = fold(uncertainties=(uncertainty(),))
    assert result == Blocked("execution_unknown", (("uncertainty", str(UUID(int=500))),))


def test_resolution_waits_for_new_accepted_snapshot() -> None:
    fact = replace(attempt(1, "400", "unknown", seq=5), resolution="not_sent")
    basis = replace(BASIS, unresolved_attempts=((fact.attempt_id, "fUSD"),))
    result = fold(basis=basis, attempts=(fact,), uncertainties=(uncertainty(is_open=False),))
    assert result == Blocked("execution_unknown", (("basis", "unresolved"),))
    next_basis = replace(basis, snapshot_seq=25, command_fence=24, unresolved_attempts=(),
                         settled_attempts=frozenset({fact.attempt_id}))
    available = fold(basis=next_basis, attempts=(fact,),
                     context=replace(CONTEXT, latest_observation_seq=25))
    assert isinstance(available, Available)
    assert available.view.snapshot.unreflected_commitments == D("0")


def test_post_fence_proven_not_accepted_resolution_keeps_original_unknown() -> None:
    fact = replace(attempt(1, "400", "unknown"), resolution="not_sent")
    result = fold(attempts=(fact,), uncertainties=(uncertainty(is_open=False),))
    assert isinstance(result, Available)
    assert result.view.snapshot.unreflected_commitments == D("0")
    assert fact.outcome == "unknown"


def test_matched_unknown_retains_transport_outcome_and_counts_commitment() -> None:
    fact = replace(attempt(1, "400", "unknown"), resolution="acknowledged")
    result = fold(attempts=(fact,), uncertainties=(uncertainty(is_open=False),))
    assert isinstance(result, Available)
    assert result.view.snapshot.unreflected_commitments == D("400")
    assert result.view.snapshot.cell_exposure == D("750")
    assert fact.outcome == "unknown"
    assert fact.resolution == "acknowledged"


def test_partial_fill_counts_remaining_and_credit_without_original_commitment() -> None:
    original = attempt(1, "500", "acknowledged", seq=5)
    values = SymbolCapital("fUSD", D("500"), D("200"), D("300"), D("0"), D("0"),
                           (("alpha", D("500")),))
    basis = replace(BASIS, symbols=(values,), reflected_attempts=frozenset({original.attempt_id}))
    result = fold(basis=basis, attempts=(original,))
    assert isinstance(result, Available)
    assert result.view.snapshot == CapitalSnapshot(D("500"), D("0"), D("1000"), D("500"))
    assert result.view.budget == CapitalBudget(D("400"), D("450"), D("0"), D("0"),
                                               "cell_headroom_exhausted")


def test_reflected_terminal_offer_never_resurrects_original_amount() -> None:
    original = attempt(1, "500", "acknowledged", seq=5)
    values = replace(VALUES, available=D("1000"), offered=D("0"), credits=D("0"),
                     unattributed_credits=D("0"), cells=())
    basis = replace(BASIS, symbols=(values,), reflected_attempts=frozenset({original.attempt_id}))
    result = fold(basis=basis, attempts=(original,))
    assert isinstance(result, Available)
    assert result.view.snapshot == CapitalSnapshot(D("1000"), D("0"), D("1000"), D("0"))


def test_foreign_offers_and_unattributed_credit_are_not_cell_exposure() -> None:
    values = replace(VALUES, cells=(), credits=D("300"), unattributed_credits=D("300"),
                     offered=D("0"), foreign_offers=D("9999"))
    result = fold(basis=replace(BASIS, symbols=(values,)))
    assert isinstance(result, Available)
    assert result.view.snapshot == CapitalSnapshot(D("1000"), D("0"), D("1300"), D("0"))
    assert result.view.unattributed_credit_exposure == D("300")
    assert result.view.budget.cell_headroom == D("600")


def test_ambiguous_credit_counts_once_in_total_and_fully_in_each_candidate_cell() -> None:
    values = replace(VALUES, offered=D("0"), unattributed_credits=D("0"),
                     cells=(("alpha", D("300")), ("beta", D("300"))))
    for cell in ("alpha", "beta"):
        result = fold(scope=replace(SCOPE, cell_id=cell), basis=replace(BASIS, symbols=(values,)))
        assert isinstance(result, Available)
        assert result.view.snapshot.total_capital == D("1300")
        assert result.view.snapshot.cell_exposure == D("300")


def test_old_classification_without_credit_cells_charges_u_to_each_cell() -> None:
    result = fold(basis=replace(BASIS, credit_cells_present=False))
    assert isinstance(result, Available)
    assert result.view.snapshot.cell_exposure == D("400")
    assert result.view.snapshot.total_capital == D("1500")


@pytest.mark.parametrize(("enabled", "reserve", "pending", "expected"), [
    (False, "100", "0", CapitalBudget(D("0"), D("0"), D("0"), D("0"), "policy_disabled")),
    (True, "2000", "0", CapitalBudget(D("0"), D("0"), D("0"), D("0"),
                                       "insufficient_deployable_funds")),
    (True, "100", "900", CapitalBudget(D("0"), D("700"), D("0"), D("0"),
                                        "insufficient_deployable_funds")),
    (True, "100", "400", CapitalBudget(D("500"), D("700"), D("0"), D("0"),
                                        "cell_headroom_exhausted")),
])
def test_policy_reserve_disabled_clamps_and_reason_precedence(
    enabled: bool, reserve: str, pending: str, expected: CapitalBudget,
) -> None:
    policy = replace(POLICY, policy=replace(POLICY.policy, enabled=enabled, reserve_amount=D(reserve)))
    result = fold(policy=policy, attempts=(attempt(1, pending),))
    assert isinstance(result, Available)
    assert result.view.budget == expected
    assert result.view.budget == evaluate_capital(policy.policy, result.view.snapshot)


@pytest.mark.parametrize(("start", "finish", "now", "allowed"), [
    (1000, 1100, 1500, True), (1000, 1100, 1501, False),
    (1201, 1201, 1200, False), (1000, 1201, 1200, False),
    (1100, 1000, 1200, False), (1200, 1200, 1200, True),
])
def test_freshness_uses_start_and_inclusive_bounds(start: int, finish: int, now: int,
                                                 allowed: bool) -> None:
    result = fold(basis=replace(BASIS, query_started_at_ms=start, query_finished_at_ms=finish),
                  context=replace(CONTEXT, now_ms=now))
    if allowed:
        assert isinstance(result, Available)
    else:
        assert result == Blocked("snapshot_stale", ())


@pytest.mark.parametrize(("context", "reason"), [
    (replace(CONTEXT, latest_query_id=UUID(int=99)), "snapshot_query_pending"),
    (replace(CONTEXT, latest_observation_seq=21), "snapshot_superseded_by_unfenced_observation"),
    (replace(CONTEXT, read_watermark=19), "snapshot_evidence_conflict"),
])
def test_read_heads(context: CapitalReadContext, reason: str) -> None:
    result = fold(context=context)
    assert isinstance(result, Blocked)
    assert result.reason == reason


def test_missing_symbol_does_not_mean_zero_capital() -> None:
    assert fold(basis=replace(BASIS, symbols=())) == Blocked("snapshot_symbol_missing",
                                                            (("symbol", "fUSD"),))


@pytest.mark.parametrize("source", ["basis", "policy", "attempt", "uncertainty"])
@pytest.mark.parametrize("field", ["account_id", "environment"])
def test_cross_scope_inputs_fail_closed(source: str, field: str) -> None:
    changes = {field: UUID(int=99) if field == "account_id" else "other"}
    if source == "basis":
        result = fold(basis=replace(BASIS, **changes))
    elif source == "policy":
        result = fold(policy=replace(POLICY, **changes))
    elif source == "attempt":
        result = fold(attempts=(replace(attempt(1, "100"), scope=replace(SCOPE, **changes)),))
    else:
        result = fold(uncertainties=(replace(uncertainty(), **changes),))
    assert isinstance(result, Blocked)


def test_wrong_policy_symbol_is_blocked() -> None:
    assert fold(policy=replace(POLICY, symbol="fUST")) == Blocked(
        "inconsistent_policy_pointer", (("scope", "policy"),),
    )


@pytest.mark.parametrize("reason", ["snapshot_prefix_diverged", "snapshot_confirmation_missing",
                                     "snapshot_incomplete", "attempt_projection_missing",
                                     "attempt_outcome_evidence_conflict"])
def test_loader_integrity_failure_preserves_reason_and_evidence(reason: str) -> None:
    failure = Blocked(reason, (("event", "20"),))
    assert fold(context=replace(CONTEXT, evidence_block=failure)) is failure


def test_acceptance_block_is_not_overridden_by_valid_amounts() -> None:
    failure = Blocked("unclassifiable_commitment", (("intent", "5"),))
    assert fold(basis=replace(BASIS, authorization_block=failure)) is failure


@pytest.mark.parametrize("seq", [5, 10, 31])
def test_unaccounted_prefence_or_future_intent_is_not_silently_dropped(seq: int) -> None:
    result = fold(attempts=(attempt(1, "100", seq=seq),))
    assert isinstance(result, Blocked)
    assert result.reason == ("unclassifiable_commitment" if seq <= 10
                             else "attempt_intent_scope_conflict")


def test_duplicate_intent_is_integrity_failure() -> None:
    fact = attempt(1, "100")
    result = fold(attempts=(fact, fact))
    assert result == Blocked("duplicate_attempt_intent", (("attempt", str(fact.attempt_id)),))


def test_frozen_contracts_and_repeatable_fold() -> None:
    facts = (attempt(1, "123.456789"),)
    first = fold(attempts=facts)
    assert first == fold(attempts=facts)
    with pytest.raises(FrozenInstanceError):
        BASIS.snapshot_seq = 99
    with pytest.raises(FrozenInstanceError):
        facts[0].amount = D("0")
    assert BASIS.snapshot_seq == 20
    assert facts[0].amount == D("123.456789")


@pytest.mark.parametrize("bad", [D("-1"), D("NaN"), D("Infinity"), 1.0, "1"])
def test_amounts_are_finite_nonnegative_decimals(bad: object) -> None:
    with pytest.raises(ValueError, match="finite, non-negative Decimal"):
        replace(VALUES, offered=bad)
    with pytest.raises(ValueError, match="finite, non-negative Decimal"):
        replace(attempt(1, "1"), amount=bad)


def test_comparison_vocabulary_keeps_input_gaps_out_of_equal_by_name() -> None:
    assert set(get_args(ComparisonKind)) == {"fold_comparison", "acceptance_rederivation"}
    assert set(get_args(ComparisonStatus)) == {"equal", "different", "not_comparable", "error"}
    assert set(get_args(DifferenceClassification)) == {
        "fence_changed", "query_pending", "post_fence_commitment", "unknown_open",
        "resolved_waiting_snapshot", "unknown_match_evidence_gap", "foreign_vs_legacy_quarantine",
        "partial_fill", "filled_before_first_snapshot", "credit_close_observation_lag",
        "credit_attribution_evidence_arrival", "historical_cid_cycle", "projection_integrity",
        "sampling_or_input_gap", "input_evidence_gap",
    }


def test_open_uncertainty_precedes_evidence_failure() -> None:
    failure = Blocked("snapshot_prefix_diverged", (("event", "20"),))
    result = fold(uncertainties=(uncertainty(),),
                  context=replace(CONTEXT, evidence_block=failure))
    assert result == Blocked("execution_unknown", (("uncertainty", str(UUID(int=500))),))


def test_policy_failure_precedes_open_uncertainty() -> None:
    failure = Blocked("inconsistent_policy_pointer", (("revision", "7"),))
    result = fold(uncertainties=(uncertainty(),),
                  context=replace(CONTEXT, policy_block=failure))
    assert result is failure


@pytest.mark.parametrize(("failure_reason", "query_pending", "authorization", "stale", "expected"), [
    ("snapshot_unavailable", True, False, False, "snapshot_unavailable"),
    ("snapshot_prefix_diverged", True, False, False, "snapshot_query_pending"),
    ("snapshot_prefix_diverged", False, True, False, "snapshot_prefix_diverged"),
    ("snapshot_confirmation_missing", False, True, False, "unclassifiable_commitment"),
    ("snapshot_confirmation_missing", False, False, True, "snapshot_confirmation_missing"),
    ("snapshot_incomplete", False, False, True, "snapshot_incomplete"),
    ("snapshot_conflicting_identity", False, False, True, "snapshot_stale"),
    ("attempt_projection_missing", False, False, True, "snapshot_stale"),
    ("unclassifiable_commitment", False, False, True, "snapshot_stale"),
])
def test_evidence_checks_preserve_authority_order(
    failure_reason: str, query_pending: bool, authorization: bool, stale: bool, expected: str,
) -> None:
    context = replace(CONTEXT, evidence_block=Blocked(failure_reason, ()),
                      latest_query_id=UUID(int=99) if query_pending else BASIS.query_id,
                      now_ms=2000 if stale else CONTEXT.now_ms)
    basis = replace(BASIS, authorization_block=(
        Blocked("unclassifiable_commitment", ()) if authorization else None
    ))
    result = fold(basis=basis, context=context)
    assert isinstance(result, Blocked)
    assert result.reason == expected
