"""Cutover comparison pieces without a database: field map, F7 classifier, inputs, window, exit.

Mutations (apply one, run this file, revert):

* ``declare_divergence`` returns its evidence without re-checking the adjusted legacy answer:
  ``test_f7_declares_only_an_exact_explanation[*]``;
* ``finish_cutover`` drops the window, the inventory or an arm from the exit decision:
  ``test_exit_needs_every_arm_the_inventory_and_the_window``;
* ``_both`` reports one direction only: ``test_anti_join_reports_both_directions``;
* the as-of check in ``cutover_inputs`` removed: ``test_cutover_inputs_are_checked_before_connecting``.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.apps import capital_comparison as command
from bfx_funding_bot.apps.capital_comparison_closure import (
    CHECKS,
    ClosureCheck,
    EvidenceRejectedError,
    _both,
    parse_seed_evidence,
    summarize_closure,
)
from bfx_funding_bot.apps.capital_comparison_guard import ConnectionPlan, GuardRejectedError
from bfx_funding_bot.apps.capital_comparison_ledger import (
    COMPARED_FIELDS,
    EXCLUDED_FIELDS,
    ArmResult,
    CreditGroup,
    declare_divergence,
    differences,
    ledger_fields,
    legacy_fields,
    summarize_arm,
)
from bfx_funding_bot.modules.execution.capital_observed_baseline import (
    OBSERVATION_FORMAT,
    OBSERVATION_VERSION,
    ObservationRejectedError,
    ObservedAvailable,
    observed_as_of_ms,
    parse_observations,
    window_start_ms,
)
from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from bfx_funding_bot.modules.trading import (
    AcceptedCapitalBasis,
    AppliedPolicy,
    Available,
    Blocked,
    CapitalPolicy,
    CapitalScope,
    CapitalSnapshot,
    CapitalView,
    evaluate_capital,
)
from bfx_funding_bot.modules.trading_shadow._internal.comparison import _result_fields

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
REVISION_ID = uuid4()
POLICY = CapitalPolicy(enabled=True, reserve_amount=Decimal("0"), max_cell_fraction=Decimal("0.7"))
A, B = "fUST_a30", "fUST_b60"
SCOPE_B = CapitalScope(ACCOUNT, "ci", "fUST", B)


def _snapshot(exposure: str, available: str = "1000") -> CapitalSnapshot:
    return CapitalSnapshot(Decimal(available), Decimal(0), Decimal("1500"), Decimal(exposure))


def legacy(exposure: str, *, cells: list[str], unattributed: str = "0",
           available: str = "1000", basis: str = "funding_trade") -> ObservedAvailable:
    snapshot = _snapshot(exposure, available)
    return ObservedAvailable(
        ACCOUNT, "ci", "fUST", 1, "digest", REVISION_ID, POLICY, 10, snapshot,
        evaluate_capital(POLICY, snapshot), Decimal(unattributed),
        {"8002": {"symbol": "fUST", "amount": "150", "period": 2, "opening": 50,
                  "cells": cells, "basis": basis}},
        10, 10,
    )


def ledger(exposure: str, *, unattributed: str = "0", available: str = "1000") -> Available:
    snapshot = _snapshot(exposure, available)
    applied = AppliedPolicy(ACCOUNT, "ci", "fUST", 1, "digest", REVISION_ID, POLICY)
    accepted = AcceptedCapitalBasis(ACCOUNT, "ci", uuid4(), uuid4(), 0, 1, 2, (), frozenset(),
                                    frozenset(), (), (), None)
    return Available(CapitalView(applied, uuid4(), uuid4(), snapshot,
                                 evaluate_capital(POLICY, snapshot), Decimal(unattributed), accepted))


def group(cells: set[str], basis: str = "carry", amount: str = "150") -> CreditGroup:
    return CreditGroup("fUST", Decimal(amount), 2, 50, basis, frozenset(cells))


SEEDED = {("credit", "8002"): group({A, B}, "recent_fill")}


def test_field_map_is_the_fold_arms_minus_what_cannot_compare() -> None:
    fold = set(_result_fields(ledger("100"))) | {"reason"}
    assert set(COMPARED_FIELDS) == fold - {"query_id"}
    assert "query_id" in EXCLUDED_FIELDS and "attribution.command_fence" in EXCLUDED_FIELDS
    assert set(ledger_fields(ledger("100"))) == set(legacy_fields(legacy("100", cells=[A])))
    assert ledger_fields(Blocked("snapshot_stale", (("x", "y"),))) == {
        "kind": "blocked", "reason": "snapshot_stale"}


@pytest.mark.parametrize("case", [
    "exact", "extra_difference", "not_seeded_recent_fill", "ledger_cells_moved",
    "legacy_not_subset", "amount_differs", "ledger_less_conservative",
    "legacy_basis_recent_fill", "legacy_basis_partial",
])
def test_f7_declares_only_an_exact_explanation(case: str) -> None:
    """b60: ledger carries the seed's {a30, b60}; legacy attributes 8002 to a30 only."""
    old, new, seeded = legacy("100", cells=[A]), ledger("250"), SEEDED
    ledger_groups = {("credit", "8002"): group({A, B})}
    if case == "extra_difference":
        new = ledger("250", available="1001")
    elif case == "not_seeded_recent_fill":
        seeded = {("credit", "8002"): group({A, B}, "trade")}
    elif case == "ledger_cells_moved":
        ledger_groups = {("credit", "8002"): group({A, B, "fUST_p2"})}
    elif case == "legacy_not_subset":
        old = legacy("100", cells=[A, B])
    elif case == "amount_differs":
        ledger_groups = {("credit", "8002"): group({A, B}, amount="151")}
    elif case == "ledger_less_conservative":
        old, new = legacy("250", cells=[A, B]), ledger("100")
    elif case == "legacy_basis_recent_fill":  # a subset, but not legacy's funding-trade upgrade
        old = legacy("100", cells=[A], basis="recent_fill")
    elif case == "legacy_basis_partial":
        old = legacy("100", cells=[A], basis="funding_trade_partial")
    found = declare_divergence(SCOPE_B, old, ledger_fields(new), ledger_groups, seeded)
    if case == "exact":
        assert differences(ledger_fields(new), legacy_fields(old))  # it was a difference
        assert found == ({
            "credit": "credit:8002", "symbol": "fUST", "amount": "150", "period_days": 2,
            "mts_opening": 50, "seed_basis": "recent_fill", "ledger_basis": "carry",
            "ledger_cells": [A, B], "legacy_cells": [A], "legacy_basis": "funding_trade",
        },)
    else:
        assert found is None


def test_f7_moves_the_unattributed_amount_too() -> None:
    """Legacy's trade named no offer of ours (unattributed); the ledger carries the seed cells."""
    old = legacy("100", cells=[], unattributed="150")
    found = declare_divergence(SCOPE_B, old, ledger_fields(ledger("250")),
                               {("credit", "8002"): group({A, B})}, SEEDED)
    assert found is not None and found[0]["legacy_cells"] == []


def _observation(account: UUID = ACCOUNT, start: int = 1000, **changes: Any) -> dict[str, Any]:
    event = VenueSnapshotObserved(
        account_id=str(account), environment="ci", query_started_at_ms=start,
        query_finished_at_ms=start + 50, offers=(), credits=(),
        wallet_available={"fUST": Decimal(1)}, coverage=SnapshotCoverage(True, True, True),
    )
    confirmation = replace(event, query_started_at_ms=start + 50, query_finished_at_ms=start + 60,
                           event_id=uuid4(), **changes)
    return {"account_id": str(account), "environment": "ci",
            "event": serialize_event(event), "confirmation": serialize_event(confirmation),
            "ledger": {"query_id": str(uuid4()), "observation_id": str(uuid4()),
                       "first_digest": "f" * 64, "confirmation_digest": "f" * 64}}


def _file(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"format": OBSERVATION_FORMAT, "version": OBSERVATION_VERSION, "observations": list(entries)}


def test_observation_file_parses_and_names_its_window() -> None:
    (parsed,) = parse_observations(_file(_observation()))
    assert (parsed.account_id, parsed.environment) == (ACCOUNT, "ci")
    # The window starts at the FIRST observation's query start: legacy classifies that one.
    assert (window_start_ms([parsed]), observed_as_of_ms([parsed])) == (1000, 1060)
    assert parsed.ledger.first_digest == "f" * 64


def test_the_window_starts_at_the_oldest_account_and_bounds_its_first_observation() -> None:
    """R1-1: the confirmation starting within 300 s does not make an older first observation
    (or an older account) fresh."""
    older, newer = parse_observations(_file(_observation(), _observation(uuid4(), start=5000)))
    assert window_start_ms([older, newer]) == 1000
    assert observed_as_of_ms([older, newer]) == 5060
    end = 1050 + 300_000  # within 300 s of the confirmation's start, not of the first query
    summary = {"kind": "summary", "inventory_status": "ok",
               "arms": {"ledger_reader": {"passed": True}, "closure": {"passed": True}}}
    finished = command.finish_cutover(summary, window_start=window_start_ms([older]), now=end,
                                      limit_ms=300_000)
    assert finished["exit_code"] == 1 and finished["window"]["elapsed_ms"] == 300_050


@pytest.mark.parametrize("fault,reason", [
    ("format", "observation_file_invalid"),
    ("empty", "observation_file_invalid"),
    ("extra_key", "observation_entry_invalid"),
    ("environment", "observation_entry_invalid"),
    ("scope", "observation_scope_mismatch"),
    ("duplicate", "observation_scope_duplicate"),
    ("acceptance", "observation_carries_acceptance"),
    ("unversioned", "observation_event_invalid"),
    ("ledger_missing", "observation_entry_invalid"),
    ("ledger_id_invalid", "observation_entry_invalid"),
])
def test_observation_file_refusals(fault: str, reason: str) -> None:
    entry = _observation()
    payload: Any = _file(entry)
    if fault == "format":
        payload["format"] = "other"
    elif fault == "empty":
        payload["observations"] = []
    elif fault == "extra_key":
        entry["extra"] = 1
    elif fault == "environment":
        entry["environment"] = "staging"
    elif fault == "scope":
        entry["account_id"] = str(uuid4())
    elif fault == "duplicate":
        payload["observations"].append(_observation())
    elif fault == "acceptance":
        payload = _file(_observation(capital_query_id=str(uuid4())))
    elif fault == "unversioned":
        del entry["event"]["__schema_version__"]
    elif fault == "ledger_missing":
        del entry["ledger"]
    elif fault == "ledger_id_invalid":
        entry["ledger"]["query_id"] = "not-a-uuid"
    with pytest.raises(ObservationRejectedError) as raised:
        parse_observations(json.loads(json.dumps(payload)))
    assert raised.value.reason == reason


def _seed_lines(**summary: Any) -> list[str]:
    scope = {"account_id": str(ACCOUNT), "environment": "ci"}
    lines = [
        {"kind": "seed", "scope": scope, "observation_id": str(uuid4()), "basis_id": str(uuid4()),
         "watermarks": {"legacy_final_event_seq": 9, "snapshot_event_seq": 8,
                        "snapshot_query_id": str(uuid4()), "trading_state_max_id": 2},
         "failed_uncertainty_requests": [str(uuid4())],
         "carried_requests": {"capital_policy_requests": [], "trading_control_requests": []}},
        {"kind": "verification", "scope": scope, "mismatches": []},
        {"kind": "summary", "exit_code": 0, "committed": True, **summary},
    ]
    return [json.dumps(line) for line in lines]


def test_seed_evidence_parses_only_a_committed_verified_seed() -> None:
    (evidence,) = parse_seed_evidence(_seed_lines()).values()
    assert (evidence.account_id, evidence.trading_state_max_id) == (ACCOUNT, 2)
    assert len(evidence.failed_uncertainty_requests) == 1
    with pytest.raises(EvidenceRejectedError, match="seed_not_committed"):
        parse_seed_evidence(_seed_lines(committed=False))
    unverified = _seed_lines()
    unverified[1] = unverified[1].replace('"mismatches": []', '"mismatches": [{"table": "x"}]')
    with pytest.raises(EvidenceRejectedError, match="seed_not_verified"):
        parse_seed_evidence(unverified)
    with pytest.raises(EvidenceRejectedError, match="seed_not_verified"):
        parse_seed_evidence([_seed_lines()[0], _seed_lines()[2]])
    with pytest.raises(EvidenceRejectedError, match="seed_evidence_invalid"):
        parse_seed_evidence(["{not json"])


def test_anti_join_reports_both_directions() -> None:
    found = _both("left_only", "right_only", {"a", "b"}, {"b", "c"})
    assert [(v["reason"], v["item"]) for v in found] == [("left_only", "a"), ("right_only", "c")]


SCOPES = [CapitalScope(ACCOUNT, "ci", "fUST", A), SCOPE_B]


def _summary(*, ledger_passed: bool = True, closure_passed: bool = True,
             inventory: str = "ok") -> dict[str, object]:
    return {"kind": "summary", "inventory_status": inventory, "arms": {
        "ledger_reader": {"passed": ledger_passed}, "closure": {"passed": closure_passed}}}


def test_exit_needs_every_arm_the_inventory_and_the_window() -> None:
    def code(**kwargs: Any) -> int:
        now = kwargs.pop("now", 1000 + 300_000)
        return int(str(command.finish_cutover(_summary(**kwargs), window_start=1000, now=now,
                                              limit_ms=300_000)["exit_code"]))

    assert code() == 0
    assert code(now=1000 + 300_001) == 1
    assert code(now=999) == 1  # a window that ends before it starts is not a window
    assert code(ledger_passed=False) == 1
    assert code(closure_passed=False) == 1
    assert code(inventory="not_comparable") == 1


def _arm(scope: CapitalScope, status: str, ledger_value: dict[str, object] | None = None) -> ArmResult:
    return ArmResult("ledger_reader", scope, status, ledger_value, ledger_value)  # type: ignore[arg-type]


def test_arm_summary_counts_declared_as_passing_and_needs_every_scope() -> None:
    available = {"kind": "available"}
    assert summarize_arm(SCOPES, [_arm(SCOPES[0], "equal", available),
                                  _arm(SCOPES[1], "declared", available)])["passed"]
    assert not summarize_arm(SCOPES, [_arm(SCOPES[0], "equal", available)])["passed"]
    assert not summarize_arm(SCOPES, [_arm(SCOPES[0], "equal", available),
                                      _arm(SCOPES[1], "different", available)])["passed"]
    stale = {"kind": "blocked", "reason": "snapshot_stale"}
    inconclusive = summarize_arm(SCOPES, [_arm(s, "equal", stale) for s in SCOPES])
    assert inconclusive["inconclusive"] and not inconclusive["passed"]
    unknown = {"kind": "blocked", "reason": "execution_unknown"}
    assert summarize_arm(SCOPES, [_arm(s, "equal", unknown) for s in SCOPES])["passed"]
    assert not summarize_arm([], [])["passed"]


def test_closure_summary_needs_every_check_for_every_scope() -> None:
    checks = [ClosureCheck(name, ACCOUNT, "ci") for name in CHECKS]
    assert summarize_closure(SCOPES, checks)["passed"]
    assert not summarize_closure(SCOPES, checks[:-1])["passed"]
    failing = [*checks[:-1], ClosureCheck(CHECKS[-1], ACCOUNT, "ci", ({"reason": "x"},))]
    assert summarize_closure(SCOPES, failing) == {
        "checks": len(CHECKS), "violations": 1, "coverage_complete": True, "passed": False}


def test_cutover_inputs_are_checked_before_connecting(tmp_path: Path) -> None:
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps(_file(_observation())))
    evidence = tmp_path / "seed.jsonl"
    evidence.write_text("\n".join(_seed_lines()))

    def inputs(now_ms: int = 1060, *, scopes: list[CapitalScope] = SCOPES,
               observed: Path | None = observation) -> command.CutoverInputs:
        args = argparse.Namespace(observation=observed, seed_evidence=evidence)
        plan = ConnectionPlan(None, "reader", "db", "run", now_ms, "cutover")  # type: ignore[arg-type]
        return command.cutover_inputs(args, plan, scopes)

    assert inputs().window_start_ms == 1000
    for kwargs, reason in (
        ({"now_ms": 1059}, "as_of_mismatch"),
        ({"now_ms": 1061}, "as_of_mismatch"),
        ({"scopes": [CapitalScope(uuid4(), "ci", "fUST", A)]}, "observation_scope_mismatch"),
        ({"observed": None}, "cutover_inputs_required"),
    ):
        with pytest.raises(GuardRejectedError) as raised:
            inputs(**kwargs)  # type: ignore[arg-type]
        assert raised.value.reason == reason


def test_the_verifier_states_the_seed_contract_independently() -> None:
    """The verifier never imports the seed (dormancy), so its statement of the contract is
    pinned equal to the seed's here."""
    from bfx_funding_bot.apps.capital_comparison_closure import (
        LEDGER_ATTRIBUTION_OF_LEGACY,
        LEDGER_OUTCOME_OF_LEGACY,
        SUPERSEDED_REASON,
    )
    from bfx_funding_bot.apps.capital_comparison_ledger import legacy_credit_key
    from bfx_funding_bot.modules.execution.ledger_seed import (
        LEGACY_ATTRIBUTION,
        LEGACY_OUTCOMES,
        credit_identity,
    )
    from bfx_funding_bot.modules.ledger.seed import SUPERSEDED_REASON as SEED_REASON

    assert dict(LEGACY_ATTRIBUTION) == LEDGER_ATTRIBUTION_OF_LEGACY
    assert dict(LEGACY_OUTCOMES) == LEDGER_OUTCOME_OF_LEGACY
    assert SUPERSEDED_REASON == SEED_REASON
    for credit_id in ("123", "loan:9"):
        assert legacy_credit_key(credit_id) == credit_identity(credit_id)
    assert legacy_credit_key("loan:") is None and legacy_credit_key("") is None
