"""Pure, native-currency limits for new offers from an explicit applied policy.

Callers supply one canonical snapshot with deduplicated exposures/commitments.
Scope, freshness, revision fencing and venue eligibility belong to the caller;
this evaluator neither quantizes amounts nor authorizes a financial operation.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

CapitalBlockedReason = Literal[
    "policy_disabled", "insufficient_deployable_funds", "cell_headroom_exhausted"
]

_ZERO = Decimal("0")


def _validate_amount(name: str, value: Decimal) -> None:
    # Dataclass annotations alone do not enforce runtime types. In particular,
    # do not convert floats, strings, integers or bools to Decimal here.
    if not isinstance(value, Decimal) or not value.is_finite() or value < _ZERO:
        raise ValueError(f"{name} must be a finite, non-negative Decimal")


@dataclass(frozen=True, slots=True)
class CapitalPolicy:
    """Validated explicit policy; defaults never substitute for a missing policy."""

    enabled: bool
    reserve_amount: Decimal = _ZERO
    allocation_mode: Literal["all_available"] = "all_available"
    max_cell_fraction: Decimal = Decimal("0.70")

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a bool")
        _validate_amount("reserve_amount", self.reserve_amount)
        if self.allocation_mode != "all_available":
            raise ValueError("allocation_mode must be all_available")
        _validate_amount("max_cell_fraction", self.max_cell_fraction)
        if not _ZERO < self.max_cell_fraction <= Decimal("1"):
            raise ValueError("max_cell_fraction must be greater than 0 and at most 1")


@dataclass(frozen=True, slots=True)
class CapitalSnapshot:
    """Same-scope funding capital, with local commitments not added to total."""

    available_amount: Decimal
    unreflected_commitments: Decimal
    total_capital: Decimal
    cell_exposure: Decimal

    def __post_init__(self) -> None:
        _validate_amount("available_amount", self.available_amount)
        _validate_amount("unreflected_commitments", self.unreflected_commitments)
        _validate_amount("total_capital", self.total_capital)
        _validate_amount("cell_exposure", self.cell_exposure)
        if self.available_amount > self.total_capital:
            raise ValueError("available_amount must not exceed total_capital")


@dataclass(frozen=True, slots=True)
class CapitalProbation:
    """Reduced cell limit while a new build or a resume is on probation (ADR D3).

    The cell limit becomes ``multiplier`` of the normal one, but never below
    ``floor`` -- one venue-minimum offer -- and never above the normal limit.
    """

    multiplier: Decimal
    floor: Decimal

    def __post_init__(self) -> None:
        _validate_amount("multiplier", self.multiplier)
        _validate_amount("floor", self.floor)
        if not _ZERO < self.multiplier <= Decimal("1"):
            raise ValueError("multiplier must be greater than 0 and at most 1")


@dataclass(frozen=True, slots=True)
class CapitalBudget:
    """Diagnostic limits plus the maximum new amount; None means positive budget.

    Disabled policies zero every amount. Exhausted budgets preserve diagnostic
    limits but have max_new_offer=0. Cash exhaustion precedes cell exhaustion when
    both apply. No venue minimum is assumed by this pure capital calculation.
    """

    spendable: Decimal
    cell_limit: Decimal
    cell_headroom: Decimal
    max_new_offer: Decimal
    reason: CapitalBlockedReason | None

    def __post_init__(self) -> None:
        _validate_amount("spendable", self.spendable)
        _validate_amount("cell_limit", self.cell_limit)
        _validate_amount("cell_headroom", self.cell_headroom)
        _validate_amount("max_new_offer", self.max_new_offer)
        if self.reason not in (
            None,
            "policy_disabled",
            "insufficient_deployable_funds",
            "cell_headroom_exhausted",
        ):
            raise ValueError("reason must be a supported capital blocked reason or None")


def evaluate_capital(policy: CapitalPolicy, snapshot: CapitalSnapshot,
                     probation: CapitalProbation | None = None) -> CapitalBudget:
    """Evaluate new-offer limits without I/O, mutation or implicit input defaults."""
    if not isinstance(policy, CapitalPolicy):
        raise ValueError("policy must be an explicit validated CapitalPolicy")
    if not isinstance(snapshot, CapitalSnapshot):
        raise ValueError("snapshot must be an explicit validated CapitalSnapshot")
    if probation is not None and not isinstance(probation, CapitalProbation):
        raise ValueError("probation must be an explicit validated CapitalProbation")
    if not policy.enabled:
        return CapitalBudget(_ZERO, _ZERO, _ZERO, _ZERO, "policy_disabled")

    spendable = max(
        _ZERO,
        snapshot.available_amount - snapshot.unreflected_commitments - policy.reserve_amount,
    )
    cell_limit = (
        max(_ZERO, snapshot.total_capital - policy.reserve_amount) * policy.max_cell_fraction
    )
    if probation is not None:
        cell_limit = min(cell_limit, max(cell_limit * probation.multiplier, probation.floor))
    cell_headroom = max(_ZERO, cell_limit - snapshot.cell_exposure)
    max_new_offer = min(spendable, cell_headroom)
    reason: CapitalBlockedReason | None = None
    if spendable == _ZERO:
        reason = "insufficient_deployable_funds"
    elif cell_headroom == _ZERO:
        reason = "cell_headroom_exhausted"
    return CapitalBudget(spendable, cell_limit, cell_headroom, max_new_offer, reason)
