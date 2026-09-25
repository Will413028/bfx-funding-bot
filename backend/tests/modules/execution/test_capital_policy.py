from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.execution.capital_policy import (
    CapitalBudget,
    CapitalPolicy,
    CapitalSnapshot,
    evaluate_capital,
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


# ------------------------------------------------------------ probation (D3)


def _snapshot_for_probation(total: str, exposure: str = "0"):
    from bfx_funding_bot.modules.execution.capital_policy import CapitalSnapshot
    return CapitalSnapshot(available_amount=Decimal(total), unreflected_commitments=Decimal("0"),
                           total_capital=Decimal(total), cell_exposure=Decimal(exposure))


@pytest.mark.parametrize(("total", "exposure", "floor", "cell_limit", "max_new"), [
    ("1000", "0", "150", "175", "175"),      # 25% of the 700 normal limit
    ("1000", "100", "150", "175", "75"),     # exposure counts against the reduced limit
    ("400", "0", "150", "150", "150"),       # 25% of 280 = 70 < one minimum offer
    ("100", "0", "150", "70", "70"),         # never above the normal limit
])
def test_probation_scales_the_cell_limit_with_a_minimum_offer_floor(total, exposure, floor,
                                                                    cell_limit, max_new) -> None:
    from bfx_funding_bot.modules.execution.capital_policy import (
        CapitalPolicy,
        CapitalProbation,
        evaluate_capital,
    )
    policy = CapitalPolicy(enabled=True, max_cell_fraction=Decimal("0.70"))
    budget = evaluate_capital(policy, _snapshot_for_probation(total, exposure),
                              CapitalProbation(multiplier=Decimal("0.25"), floor=Decimal(floor)))
    assert budget.cell_limit == Decimal(cell_limit)
    assert budget.max_new_offer == Decimal(max_new)
    normal = evaluate_capital(policy, _snapshot_for_probation(total, exposure))
    assert budget.cell_limit <= normal.cell_limit


@pytest.mark.parametrize("multiplier", ["0", "1.01", "-0.25"])
def test_probation_multiplier_is_bounded(multiplier: str) -> None:
    from bfx_funding_bot.modules.execution.capital_policy import CapitalProbation
    with pytest.raises(ValueError):
        CapitalProbation(multiplier=Decimal(multiplier), floor=Decimal("150"))
