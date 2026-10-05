"""``plan_seed``: the rows of the legacy closure seed, before any database (pure).

What it pins: every planned row has exactly the table's canonical digest columns; ids are a
pure function of the closure; the seed basis carries the legacy groups, cells and attempt
classes as given with ``attempt_seq_high_water`` = the highest seeded sequence; and each
closure the first real basis could not judge truthfully is refused, never patched.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.ledger import (
    Scope,
    SeedAttempt,
    SeedClosure,
    SeedCredit,
    SeedCreditGroup,
    SeedOffer,
    SeedOutcome,
    SeedQuarantine,
    SeedQuarantineMember,
    SeedRefused,
    SeedSymbol,
    SeedWatermarks,
)
from bfx_funding_bot.modules.ledger.seed import expected_digests, plan_seed
from bfx_funding_bot.modules.ledger.table_digest import CANONICAL_COLUMNS, DIGEST_TABLES

SCOPE = Scope(UUID("11111111-1111-1111-1111-111111111111"), "ci")
A_LIVE = UUID("00000000-0000-0000-0000-0000000000a1")
A_SETTLED = UUID("00000000-0000-0000-0000-0000000000a2")
A_UNKNOWN = UUID("00000000-0000-0000-0000-0000000000a3")


def attempt(attempt_id: UUID, seq: int, classification: str, kind: str = "ack",
            venue: str | None = "o-1", amount: str = "300", cell: str = "fUST_a30") -> SeedAttempt:
    return SeedAttempt(
        attempt_id, f"decision-{seq}", "fUST", cell, seq,
        {"symbol": "fUST", "amount": amount, "rate": "0.0001", "period": 2}, 1_000 + seq,
        SeedOutcome(kind, venue, None, 1_100 + seq, {}),  # type: ignore[arg-type]
        classification, {"legacy": "submission_attempt", "attempt_id": str(attempt_id)},  # type: ignore[arg-type]
    )


def closure(**changes: object) -> SeedClosure:
    base = SeedClosure(
        scope=SCOPE,
        watermarks=SeedWatermarks(90, 80, UUID(int=7), 79, 3),
        query_started_at_ms=5_000,
        query_finished_at_ms=5_050,
        confirmation_finished_at_ms=5_100,
        offers=(
            SeedOffer("o-1", "fUST", Decimal("300"), Decimal("200"), Decimal("0.0001"), 2,
                      "LIMIT", None, "partially_filled", 1_100, 1_200),
            SeedOffer("o-foreign", "fUST", Decimal("50"), Decimal("50"), None, None, None,
                      None, "active", 1_150, None),
        ),
        credits=(
            SeedCredit("credit", "c-1", "fUST", Decimal("100"), Decimal("0.0001"), 2, "active",
                       None, 1_300, 1_300, 1_250),
            SeedCredit("loan", "l-1", "fUST", Decimal("40"), None, 2, "active", None, None,
                       None, 1_260),
        ),
        symbols=(
            SeedSymbol("fUST", Decimal("610"), Decimal("200"), Decimal("140"), Decimal("40"),
                       Decimal("50"), {"fUST_a30": Decimal("300")}),
        ),
        credit_groups=(
            SeedCreditGroup("credit", "c-1", "fUST", Decimal("100"), 2, 1_250, "recent_fill",
                            frozenset({"fUST_a30"})),
            SeedCreditGroup("loan", "l-1", "fUST", Decimal("40"), 2, 1_260, "unattributed",
                            frozenset()),
        ),
        attempts=(
            attempt(A_LIVE, 41, "reflected"),
            attempt(A_SETTLED, 42, "settled", kind="rejected", venue=None),
            attempt(A_UNKNOWN, 57, "unresolved", kind="unknown", venue=None, amount="25"),
        ),
        quarantines=(
            SeedQuarantine(UUID(int=99), "fUST", Decimal("50"), 1_400, {"legacy": True},
                           (SeedQuarantineMember("offer", "o-foreign", Decimal("50")),)),
        ),
        evidence={"classification_digest": "d"},
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_every_planned_row_has_exactly_the_canonical_columns() -> None:
    plan = plan_seed(closure())
    assert set(plan.rows) <= set(DIGEST_TABLES)
    for name, rows in plan.rows.items():
        for row in rows:
            assert tuple(row) == CANONICAL_COLUMNS[name] or set(row) == set(CANONICAL_COLUMNS[name])
    digests = expected_digests(plan, ())
    assert set(digests) == set(DIGEST_TABLES)
    assert digests["submission_attempt_journal"].watermark == 57
    assert digests["ledger_observation_trade"].count == 0


def test_ids_are_a_function_of_the_closure() -> None:
    first, second = plan_seed(closure()), plan_seed(closure())
    assert (first.observation_id, first.basis_id, first.query_id) == (
        second.observation_id, second.basis_id, second.query_id)
    assert expected_digests(first, ()) == expected_digests(second, ())
    other = plan_seed(closure(watermarks=SeedWatermarks(90, 81, UUID(int=7), 79, 3)))
    assert other.observation_id != first.observation_id


def test_the_seed_basis_carries_the_legacy_closure_as_given() -> None:
    plan = plan_seed(closure())
    (observation,) = plan.table_rows("ledger_observation")
    assert (observation["origin"], observation["accepted"], observation["wallets_complete"],
            observation["offers_complete"], observation["trades_complete"]) == (
        "legacy_seed", True, False, True, False)
    (basis,) = plan.table_rows("accepted_capital_basis")
    assert basis["attempt_seq_high_water"] == 57
    assert basis["accept_revision"] == 0 == observation["accept_revision"]
    assert {(r["source_kind"], r["venue_credit_id"], r["attribution_basis"])
            for r in plan.table_rows("accepted_capital_basis_credit")} == {
        ("credit", "c-1", "recent_fill"), ("loan", "l-1", "unattributed")}
    assert {(r["venue_credit_id"], r["cell_id"])
            for r in plan.table_rows("accepted_capital_basis_credit_cell")} == {("c-1", "fUST_a30")}
    assert {(r["attempt_id"], r["classification"])
            for r in plan.table_rows("accepted_capital_basis_attempt")} == {
        (A_LIVE, "reflected"), (A_SETTLED, "settled"), (A_UNKNOWN, "unresolved")}
    symbol = plan.table_rows("accepted_capital_basis_symbol")[0]
    assert (symbol["conservation"], symbol["lent_unexplained"]) == ("baseline", Decimal(0))
    attempts = plan.table_rows("submission_attempt_journal")
    assert all(row["policy_revision_id"] is None and row["seed_provenance"] for row in attempts)
    assert [row["attempt_seq"] for row in attempts] == [41, 42, 57]
    (opening,) = plan.table_rows("quarantine_opening")
    assert (opening["quarantine_id"], opening["opened_revision"]) == (UUID(int=99), 1)
    (clock,) = plan.table_rows("capital_command_clock")
    assert clock["revision"] == 1  # each opening bumps, like open_quarantine
    mirrors = plan.table_rows("venue_offer_mirror")
    assert {m["venue_offer_id"] for m in mirrors} == {"o-1", "o-foreign"}
    assert all(m["present_in_latest_accepted_snapshot"] for m in mirrors)
    assert len(plan.table_rows("venue_credit_mirror")) == 2


def _refused(reason: str, **changes: object) -> None:
    with pytest.raises(SeedRefused) as raised:
        plan_seed(closure(**changes))
    assert raised.value.reason == reason


def test_a_live_offer_without_its_attempt_is_refused() -> None:
    base = closure()
    _refused("classification_inconsistent", attempts=base.attempts[1:])


def test_a_credit_group_and_its_live_credit_must_agree() -> None:
    base = closure()
    group = replace(base.credit_groups[0], mts_opening=1_251)
    _refused("credit_group_mismatch", credit_groups=(group, base.credit_groups[1]))
    _refused("credit_group_mismatch", credit_groups=base.credit_groups[:1])


def test_cells_must_add_up() -> None:
    base = closure()
    group = replace(base.credit_groups[0], cells=frozenset({"fUST_a30", "fUST_b60"}))
    _refused("classification_cells_inconsistent", credit_groups=(group, base.credit_groups[1]))


def test_an_offer_amount_other_than_the_submitted_one_is_refused() -> None:
    base = closure()
    live = replace(base.attempts[0], normalized_payload={"symbol": "fUST", "amount": "299"})
    _refused("offer_amount_conflict", attempts=(live, *base.attempts[1:]))


def test_unattributed_never_names_a_cell_and_unknown_vocabulary_is_refused() -> None:
    base = closure()
    loan = replace(base.credit_groups[1], cells=frozenset({"fUST_a30"}))
    _refused("attribution_cells_mismatch", credit_groups=(base.credit_groups[0], loan))
    loan = replace(base.credit_groups[1], attribution_basis="none")  # type: ignore[arg-type]
    _refused("attribution_basis_unknown", credit_groups=(base.credit_groups[0], loan))


def test_duplicate_sequences_and_unobserved_members_are_refused() -> None:
    base = closure()
    _refused("duplicate_attempt_seq",
             attempts=(*base.attempts, replace(base.attempts[2], attempt_id=uuid4(),
                                               execution_decision_id="x")))
    quarantine = replace(base.quarantines[0],
                         members=(SeedQuarantineMember("offer", "o-gone", Decimal(1)),))
    _refused("quarantine_member_unobserved", quarantines=(quarantine,))


def test_an_ack_must_name_its_offer() -> None:
    base = closure()
    broken = replace(base.attempts[0], outcome=SeedOutcome("ack", None, None, 1, {}))
    _refused("attempt_outcome_invalid", attempts=(broken, *base.attempts[1:]))
