from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.trading import (
    Blocked,
    CapitalBudget,
    CapitalPolicy,
    CapitalSnapshot,
    PolicyRejectedError,
    check_pointer,
    evaluate_capital,
    parse_policy,
    policy_digest,
    policy_payload,
)


@pytest.mark.parametrize(
    ("reserve", "available", "commitments", "total", "exposure", "expected", "reason"),
    [
        ("100", "1000", "200", "1200", "100", ("700", "770", "670", "670"), None),
        # Spendable cash, rather than concentration, is the binding constraint.
        ("100", "1000", "200", "2000", "100", ("700", "1330", "1230", "700"), None),
        ("100", "100", "0", "1000", "0", ("0", "630", "630", "0"), "insufficient_deployable_funds"),
        ("1500", "1000", "0", "1200", "100", ("0", "0", "0", "0"), "insufficient_deployable_funds"),
        (
            "0",
            "100",
            "200",
            "1000",
            "200",
            ("0", "700", "500", "0"),
            "insufficient_deployable_funds",
        ),
        ("0", "1000", "0", "1000", "700", ("1000", "700", "0", "0"), "cell_headroom_exhausted"),
        ("0", "1000", "0", "1000", "1200", ("1000", "700", "0", "0"), "cell_headroom_exhausted"),
        ("0", "0", "0", "0", "0", ("0", "0", "0", "0"), "insufficient_deployable_funds"),
        (
            "0.00000001",
            "0.12345678",
            "0.00000002",
            "1.00000001",
            "0.60000001",
            ("0.12345675", "0.70000000", "0.09999999", "0.09999999"),
            None,
        ),
    ],
)
def test_budget_matches_hand_derived_limits(
    reserve: str,
    available: str,
    commitments: str,
    total: str,
    exposure: str,
    expected: tuple[str, str, str, str],
    reason: str | None,
) -> None:
    budget = evaluate_capital(
        CapitalPolicy(enabled=True, reserve_amount=Decimal(reserve)),
        CapitalSnapshot(*(Decimal(v) for v in (available, commitments, total, exposure))),
    )
    actual = (budget.spendable, budget.cell_limit, budget.cell_headroom, budget.max_new_offer)
    assert actual == tuple(Decimal(v) for v in expected)
    assert all(isinstance(value, Decimal) for value in actual)
    assert budget.reason == reason


def test_explicit_policy_defaults_use_new_deposits_without_a_fixed_cap() -> None:
    policy = CapitalPolicy(enabled=True)
    before = evaluate_capital(
        policy, CapitalSnapshot(Decimal("1000"), Decimal("0"), Decimal("1000"), Decimal("100"))
    )
    after = evaluate_capital(
        policy, CapitalSnapshot(Decimal("11000"), Decimal("0"), Decimal("11000"), Decimal("100"))
    )
    assert before.max_new_offer == Decimal("600")
    assert after.max_new_offer == Decimal("7600")
    assert after.spendable == Decimal("11000")
    assert policy.allocation_mode == "all_available"


@pytest.mark.parametrize("fraction", [Decimal("0.25"), Decimal("1")])
def test_explicit_concentration_fraction_controls_limit(fraction: Decimal) -> None:
    budget = evaluate_capital(
        CapitalPolicy(enabled=True, max_cell_fraction=fraction),
        CapitalSnapshot(Decimal("100"), Decimal("0"), Decimal("100"), Decimal("0")),
    )
    assert (
        budget.max_new_offer
        == {Decimal("0.25"): Decimal("25"), Decimal("1"): Decimal("100")}[fraction]
    )


@pytest.mark.parametrize("reserve", [Decimal("0"), Decimal("2000")])
def test_disabled_policy_returns_only_zero_budget_with_disabled_reason(reserve: Decimal) -> None:
    budget = evaluate_capital(
        CapitalPolicy(enabled=False, reserve_amount=reserve),
        CapitalSnapshot(Decimal("1000"), Decimal("0"), Decimal("1000"), Decimal("0")),
    )
    assert budget == CapitalBudget(
        Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), "policy_disabled"
    )


def test_reflected_commitment_is_not_subtracted_twice() -> None:
    policy = CapitalPolicy(enabled=True, reserve_amount=Decimal("100"))
    unreflected = evaluate_capital(
        policy, CapitalSnapshot(Decimal("1000"), Decimal("200"), Decimal("1200"), Decimal("200"))
    )
    reflected = evaluate_capital(
        policy, CapitalSnapshot(Decimal("800"), Decimal("0"), Decimal("1200"), Decimal("200"))
    )
    assert unreflected == reflected
    assert reflected.spendable == Decimal("700")
    assert reflected.max_new_offer == Decimal("570")


INVALID_AMOUNTS = [
    Decimal("-0.01"),
    Decimal("NaN"),
    Decimal("sNaN"),
    Decimal("Infinity"),
    Decimal("-Infinity"),
    0,
    1,
    0.1,
    float("nan"),
    float("inf"),
    True,
    False,
    "0",
    None,
]


@pytest.mark.parametrize("value", INVALID_AMOUNTS)
def test_policy_rejects_invalid_reserve_without_numeric_coercion(value: Any) -> None:
    with pytest.raises(ValueError, match="reserve_amount"):
        CapitalPolicy(enabled=True, reserve_amount=value)


@pytest.mark.parametrize("value", [*INVALID_AMOUNTS, Decimal("0"), Decimal("1.00000001")])
def test_policy_requires_strict_finite_fraction_in_open_closed_unit_interval(value: Any) -> None:
    with pytest.raises(ValueError, match="max_cell_fraction"):
        CapitalPolicy(enabled=True, max_cell_fraction=value)


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, Decimal("1"), 1.0])
def test_policy_rejects_non_bool_enabled(value: Any) -> None:
    with pytest.raises(ValueError, match="enabled"):
        CapitalPolicy(enabled=value)


def test_enabled_is_required() -> None:
    with pytest.raises(TypeError, match="enabled"):
        CapitalPolicy()  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["fixed", "percentage", "", None, True, 1])
def test_policy_rejects_unsupported_allocation_mode(value: Any) -> None:
    with pytest.raises(ValueError, match="allocation_mode"):
        CapitalPolicy(enabled=True, allocation_mode=value)


@pytest.mark.parametrize(
    "field", ["available_amount", "unreflected_commitments", "total_capital", "cell_exposure"]
)
@pytest.mark.parametrize("value", INVALID_AMOUNTS)
def test_snapshot_rejects_invalid_quantities(field: str, value: Any) -> None:
    values = dict.fromkeys(
        ("available_amount", "unreflected_commitments", "total_capital", "cell_exposure"),
        Decimal("0"),
    )
    values[field] = value
    with pytest.raises(ValueError, match=field):
        CapitalSnapshot(**values)


def test_snapshot_rejects_available_exceeding_total() -> None:
    with pytest.raises(ValueError, match=r"available_amount.*total_capital"):
        CapitalSnapshot(Decimal("100.00000001"), Decimal("0"), Decimal("100"), Decimal("0"))


@pytest.mark.parametrize("field", ["spendable", "cell_limit", "cell_headroom", "max_new_offer"])
@pytest.mark.parametrize("value", INVALID_AMOUNTS)
def test_budget_rejects_invalid_quantities(field: str, value: Any) -> None:
    values: dict[str, Any] = dict.fromkeys(
        ("spendable", "cell_limit", "cell_headroom", "max_new_offer"), Decimal("0")
    )
    values[field] = value
    with pytest.raises(ValueError, match=field):
        CapitalBudget(**values, reason=None)


@pytest.mark.parametrize("value", ["unknown", "", True, 1])
def test_budget_rejects_unknown_reason(value: Any) -> None:
    with pytest.raises(ValueError, match="reason"):
        CapitalBudget(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), value)


@pytest.mark.parametrize(
    ("value", "field", "replacement"),
    [
        (CapitalPolicy(enabled=True), "enabled", False),
        (
            CapitalSnapshot(Decimal("1"), Decimal("0"), Decimal("1"), Decimal("0")),
            "available_amount",
            Decimal("10"),
        ),
        (
            CapitalBudget(Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), None),
            "max_new_offer",
            Decimal("10"),
        ),
    ],
)
def test_capital_inputs_and_result_are_immutable(value: Any, field: str, replacement: Any) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(value, field, replacement)


@pytest.mark.parametrize("policy", [None, {}, True])
def test_evaluator_rejects_absent_or_unvalidated_policy(policy: Any) -> None:
    with pytest.raises(ValueError, match="policy"):
        evaluate_capital(
            policy, CapitalSnapshot(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
        )


@pytest.mark.parametrize("snapshot", [None, {}, True])
def test_evaluator_rejects_absent_or_unvalidated_snapshot_even_when_disabled(snapshot: Any) -> None:
    with pytest.raises(ValueError, match="snapshot"):
        evaluate_capital(CapitalPolicy(enabled=False), snapshot)


_ACCOUNT, _REVISION_ID = uuid4(), uuid4()
_REVISION = (_REVISION_ID, _ACCOUNT, "prod", "fUST", 3)


def test_check_pointer_accepts_only_the_scope_s_own_head_revision() -> None:
    assert check_pointer(_ACCOUNT, "prod", "fUST", (_REVISION_ID, 3), _REVISION) is None
    assert check_pointer(_ACCOUNT, "prod", "fUST", None, None) == Blocked("policy_unavailable", ())
    assert check_pointer(_ACCOUNT, "prod", "fUST", None, _REVISION) == Blocked(
        "policy_unavailable", ()
    )
    inconsistent = Blocked("inconsistent_policy_pointer", ())
    assert check_pointer(_ACCOUNT, "prod", "fUST", (_REVISION_ID, 3), None) == inconsistent


@pytest.mark.parametrize("field", range(5))
def test_check_pointer_refuses_any_mismatched_identity_field(field: int) -> None:
    wrong: list[Any] = list(_REVISION)
    wrong[field] = {0: uuid4(), 1: uuid4(), 2: "paper", 3: "fUSD", 4: 4}[field]
    assert check_pointer(_ACCOUNT, "prod", "fUST", (_REVISION_ID, 3), tuple(wrong)) == Blocked(
        "inconsistent_policy_pointer", ()
    )


def _stored() -> dict[str, Any]:
    return policy_payload(CapitalPolicy(True, Decimal("100"), max_cell_fraction=Decimal("1")))


def test_parse_policy_round_trips_a_valid_revision() -> None:
    raw = _stored()
    assert parse_policy(1, raw, policy_digest(raw)) == CapitalPolicy(
        True, Decimal("100"), max_cell_fraction=Decimal("1")
    )


@pytest.mark.parametrize(
    ("schema", "mutate", "digest_ok", "reason"),
    [
        (1, None, False, "invalid_policy_schema_or_digest"),
        (999, None, True, "invalid_policy_schema_or_digest"),
        # Schema/digest is proven before keys and amounts.
        (1, "extra", False, "invalid_policy_schema_or_digest"),
        (1, "extra", True, "invalid_policy"),
        (2, None, True, "invalid_policy"),
        # A bad amount is an invalid policy, not a bare amount failure.
        (1, "nan", True, "invalid_policy"),
    ],
)
def test_parse_policy_refuses_in_authority_order(
    schema: int, mutate: str | None, digest_ok: bool, reason: str
) -> None:
    raw = _stored()
    if mutate == "extra":
        raw["extra"] = None
    elif mutate == "nan":
        raw["reserve_amount"] = "NaN"
    with pytest.raises(PolicyRejectedError) as exc:
        parse_policy(schema, raw, policy_digest(raw) if digest_ok else "bad")
    assert exc.value.reason == reason
