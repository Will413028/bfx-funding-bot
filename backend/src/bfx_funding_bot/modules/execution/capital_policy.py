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


# Bitfinex accepts funding offers of 2..120 days; a policy cannot widen this.
VENUE_MIN_PERIOD_DAYS = 2
VENUE_MAX_PERIOD_DAYS = 120


@dataclass(frozen=True, slots=True)
class OfferEnvelope:
    """The terms every new offer must stay inside (ADR 2026-09-25 lending envelope D1).

    A broken signal, sizing bug or new build can at worst lend at the floor rate for
    the longest allowed period -- that bound is what replaces release probation.
    The effective rate floor is max(min_rate_apr / 365, median live bid x ratio).
    """

    min_period_days: int
    max_period_days: int
    max_open_offers: int
    rate_floor_ratio: Decimal
    min_rate_apr: Decimal

    def __post_init__(self) -> None:
        for name in ("min_period_days", "max_period_days", "max_open_offers"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an int")
        if not (VENUE_MIN_PERIOD_DAYS <= self.min_period_days <= self.max_period_days
                <= VENUE_MAX_PERIOD_DAYS):
            raise ValueError("periods must satisfy 2 <= min_period_days <= max_period_days <= 120")
        if self.max_open_offers <= 0:
            raise ValueError("max_open_offers must be positive")
        _validate_amount("rate_floor_ratio", self.rate_floor_ratio)
        if not _ZERO < self.rate_floor_ratio <= Decimal("1"):
            raise ValueError("rate_floor_ratio must be greater than 0 and at most 1")
        _validate_amount("min_rate_apr", self.min_rate_apr)
        if not _ZERO < self.min_rate_apr < Decimal("1"):
            raise ValueError("min_rate_apr must be a fraction greater than 0 and below 1")

    @property
    def min_daily_rate(self) -> Decimal:
        return self.min_rate_apr / Decimal(365)


@dataclass(frozen=True, slots=True)
class CapitalPolicy:
    """Validated explicit policy; defaults never substitute for a missing policy."""

    enabled: bool
    reserve_amount: Decimal = _ZERO
    allocation_mode: Literal["all_available"] = "all_available"
    max_cell_fraction: Decimal = Decimal("0.70")
    # Absolute ceiling on one offer's native amount (T9, ADR 2026-09-25 D5). None
    # means never set: sizing is not capped by it, and MaxOfferAmountGuard refuses
    # every offer until an amended policy (schema 2) sets it. Not part of
    # evaluate_capital -- it bounds a single offer, not the budget.
    max_offer_amount: Decimal | None = None
    # None means never set: the offer-envelope guard refuses every offer until an
    # amended policy (schema 3) sets it together with max_offer_amount.
    envelope: OfferEnvelope | None = None

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a bool")
        _validate_amount("reserve_amount", self.reserve_amount)
        if self.allocation_mode != "all_available":
            raise ValueError("allocation_mode must be all_available")
        _validate_amount("max_cell_fraction", self.max_cell_fraction)
        if not _ZERO < self.max_cell_fraction <= Decimal("1"):
            raise ValueError("max_cell_fraction must be greater than 0 and at most 1")
        if self.max_offer_amount is not None:
            _validate_amount("max_offer_amount", self.max_offer_amount)
            if self.max_offer_amount == _ZERO:
                raise ValueError("max_offer_amount must be positive when set")
        if self.envelope is not None:
            if not isinstance(self.envelope, OfferEnvelope):
                raise ValueError("envelope must be an OfferEnvelope")
            if self.max_offer_amount is None:
                raise ValueError("an envelope requires max_offer_amount")


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
