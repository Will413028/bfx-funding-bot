"""Capital contracts and an independent, non-authorizing Decimal fold.

``AcceptedCapitalBasis`` is normalized immutable acceptance evidence, NOT raw
REST data. The contract carries no legacy event ordering: an accepted basis is
identified by its observation and query, and attempts by a per-scope monotonic
``attempt_seq``. A loader proves identity, coverage, policy and inventory
integrity, passing failures explicitly rather than fabricating facts; any
legacy-only proof (the S0 loader's event-log watermark, prefix and
confirmation checks) arrives folded into ``CapitalReadContext.integrity_block``.
There is deliberately no loader, clock, event replay or runtime wiring here.

Authority citations below are repository-relative under
``backend/src/bfx_funding_bot/modules/execution/`` at the S0 baseline.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from bfx_funding_bot.modules.trading.policy import (
    Blocked,
    CapitalBudget,
    CapitalPolicy,
    CapitalSnapshot,
    evaluate_capital,
)

ComparisonKind = Literal["fold_comparison", "acceptance_rederivation"]
ComparisonStatus = Literal["equal", "different", "not_comparable", "error"]
DifferenceClassification = Literal[
    "fence_changed", "query_pending", "post_fence_commitment", "unknown_open",
    "resolved_waiting_snapshot", "unknown_match_evidence_gap", "foreign_vs_legacy_quarantine",
    "partial_fill", "filled_before_first_snapshot", "credit_close_observation_lag",
    "credit_attribution_evidence_arrival", "historical_cid_cycle", "projection_integrity",
    "sampling_or_input_gap", "input_evidence_gap",
]
AttemptOutcome = Literal["pending", "acknowledged", "rejected", "not_sent", "unknown"]

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class CapitalScope:
    account_id: UUID
    environment: str
    symbol: str
    cell_id: str


@dataclass(frozen=True, slots=True)
class AppliedPolicy:
    """Policy and its scope/revision identity; loader proves head consistency."""

    account_id: UUID
    environment: str
    symbol: str
    revision: int
    digest: str
    revision_id: UUID
    policy: CapitalPolicy


@dataclass(frozen=True, slots=True)
class SymbolCapital:
    """Accepted A/O/C and cell totals, not venue payloads to classify again.

    O is managed *remaining* amount (capital_repository.py:483-525). C is
    deduplicated credits/loans; each candidate cell carries the full ambiguous
    credit, while U is in C only (530-546). Foreign offers are diagnostic and
    excluded from O, C and cells (488-501). ``cells`` already includes attributed
    credits; these totals must never be summed to reconstruct C or T. ``block``
    is an acceptance-time fact that makes this symbol, and only it, unusable.
    """

    symbol: str
    available: Decimal
    offered: Decimal
    credits: Decimal
    unattributed_credits: Decimal
    foreign_offers: Decimal
    cells: tuple[tuple[str, Decimal], ...]
    block: Blocked | None

    def __post_init__(self) -> None:
        for amount in (
            self.available, self.offered, self.credits, self.unattributed_credits,
            self.foreign_offers, *(amount for _, amount in self.cells),
        ):
            _validate_amount(amount)
        if self.unattributed_credits > self.credits:
            raise ValueError("unattributed credits must be included in credits")
        if len({cell for cell, _ in self.cells}) != len(self.cells):
            raise ValueError("duplicate capital cell")


@dataclass(frozen=True, slots=True)
class AcceptedCapitalBasis:
    """Complete fold basis, retaining acceptance identity and eligibility.

    ``observation_id`` and ``query_id`` identify the accepted observation;
    attempts with ``attempt_seq <= attempt_seq_high_water`` were classified by
    this acceptance. Reflected, settled and unresolved identities are accounted
    at acceptance (capital_repository.py:547-599,797-804). A resolution recorded
    after acceptance cannot clear this basis's unresolved attempts (880-883) or
    quarantines; only a new acceptance does. ``scope_block`` is an
    acceptance-time fact that blocks every symbol (849-852).
    """

    account_id: UUID
    environment: str
    observation_id: UUID
    query_id: UUID
    attempt_seq_high_water: int
    query_started_at_ms: int
    query_finished_at_ms: int
    symbols: tuple[SymbolCapital, ...]
    reflected_attempts: frozenset[UUID]
    settled_attempts: frozenset[UUID]
    unresolved_attempts: tuple[tuple[UUID, str], ...]
    unresolved_quarantines: tuple[tuple[UUID, str], ...]
    scope_block: Blocked | None


@dataclass(frozen=True, slots=True)
class AttemptFact:
    """Validated intent/decision identity, original amount and outcome evidence.

    Loader proves the full inventory, not a projection cursor (943-1006).
    ``attempt_seq`` is the attempt's per-scope monotonic position, compared
    with the basis high water. PENDING is a durable intent without an outcome.
    An UNKNOWN remains immutable; ``resolution`` records separately proven
    not-accepted or matched/bound acknowledgment evidence
    (capital_repository.py:302-350). The old authority reads matched/bound as
    acknowledged; this contract preserves the original transport outcome
    instead of reproducing that projection's overwrite.
    """

    attempt_id: UUID
    scope: CapitalScope
    attempt_seq: int
    amount: Decimal
    outcome: AttemptOutcome
    resolution: Literal["acknowledged", "not_sent"] | None = None

    def __post_init__(self) -> None:
        _validate_amount(self.amount)
        if self.outcome not in {"pending", "acknowledged", "rejected", "not_sent", "unknown"}:
            raise ValueError("invalid attempt outcome")
        if self.resolution is not None and (
            self.outcome != "unknown" or self.resolution not in {"acknowledged", "not_sent"}
        ):
            raise ValueError("resolution requires a proven acknowledged or not-accepted UNKNOWN")


@dataclass(frozen=True, slots=True)
class UncertaintyFact:
    """Current uncertainty, including legacy cases without an attempt identity."""

    uncertainty_id: UUID
    account_id: UUID
    environment: str
    symbol: str
    is_open: bool


@dataclass(frozen=True, slots=True)
class CapitalReadContext:
    """One injected clock and consistent read heads, with explicit proof failure.

    ``integrity_block`` carries the loader's first failure of any proof this
    contract cannot express (evidence identity, coverage, inventory), already
    ordered and masked by the loader. None means proven, never skipped.
    """

    now_ms: int
    max_snapshot_age_ms: int
    latest_query_id: UUID
    integrity_block: Blocked | None


@dataclass(frozen=True, slots=True)
class CapitalView:
    applied: AppliedPolicy
    query_id: UUID
    observation_id: UUID
    snapshot: CapitalSnapshot
    budget: CapitalBudget
    unattributed_credit_exposure: Decimal
    attribution: AcceptedCapitalBasis


@dataclass(frozen=True, slots=True)
class Available:
    view: CapitalView


type CapitalResult = Available | Blocked


def _validate_amount(amount: Decimal) -> None:
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < _ZERO:
        raise ValueError("capital amount must be a finite, non-negative Decimal")


def derive_capital(
    *,
    scope: CapitalScope,
    accepted: AcceptedCapitalBasis | None,
    attempts: tuple[AttemptFact, ...],
    uncertainties: tuple[UncertaintyFact, ...],
    policy: AppliedPolicy | Blocked,
    read_context: CapitalReadContext,
) -> CapitalResult:
    """Fold accepted capital and the proven tail without I/O or implicit time.

    Check order: policy for this symbol, current open uncertainty for this
    symbol, loader integrity, basis presence, query head, scope block, symbol
    block, freshness, symbol presence, basis-unresolved attempts/quarantines
    for this symbol, then the tail. L and same-cell exposure follow
    capital_repository.py:793-824. T=A+O+C excludes L. UNKNOWN is symbol-local
    (281-300), including an acceptance that still names it (880-883).
    Freshness uses query *start* and inclusive bounds (455-462). Budget
    evaluation shares the live authority's pure evaluator (822-824).
    """
    identity = (scope.account_id, scope.environment)
    # capital_repository.py:766-768 applies policy before entering _snapshot_basis.
    if isinstance(policy, Blocked):
        return policy
    if (policy.account_id, policy.environment, policy.symbol) != (*identity, scope.symbol):
        return Blocked("inconsistent_policy_pointer", (("scope", "policy"),))
    # capital_repository.py:833 checks open symbol-local uncertainty first.
    for uncertainty in uncertainties:
        if (uncertainty.account_id, uncertainty.environment) != identity:
            return Blocked("uncertainty_scope_conflict", (("id", str(uncertainty.uncertainty_id)),))
        if uncertainty.is_open and uncertainty.symbol == scope.symbol:
            return Blocked("execution_unknown", (("uncertainty", str(uncertainty.uncertainty_id)),))
    if read_context.integrity_block is not None:
        return read_context.integrity_block
    if accepted is None:
        return Blocked("snapshot_unavailable", ())
    if (accepted.account_id, accepted.environment) != identity:
        return Blocked("snapshot_evidence_conflict", (("scope", "accepted"),))
    # capital_repository.py:838-839.
    if read_context.latest_query_id != accepted.query_id:
        return Blocked("snapshot_query_pending", (("query", str(accepted.query_id)),))
    # capital_repository.py:850-853: acceptance already decided this.
    if accepted.scope_block is not None:
        return accepted.scope_block
    symbols = {values.symbol: values for values in accepted.symbols}
    if len(symbols) != len(accepted.symbols):
        return Blocked("snapshot_conflicting_identity", ())
    values = symbols.get(scope.symbol)
    if values is not None and values.block is not None:
        return values.block
    if not (
        0 <= read_context.now_ms - accepted.query_started_at_ms <= read_context.max_snapshot_age_ms
        and accepted.query_started_at_ms <= accepted.query_finished_at_ms <= read_context.now_ms
    ):
        return Blocked("snapshot_stale", ())
    # capital_repository.py:878-879.
    if values is None:
        return Blocked("snapshot_symbol_missing", (("symbol", scope.symbol),))
    # capital_repository.py:880-883: accepted unresolved survives current resolution.
    if scope.symbol in (symbol for _, symbol in accepted.unresolved_attempts):
        return Blocked("execution_unknown", (("basis", "unresolved"),))
    for quarantine_id, symbol in accepted.unresolved_quarantines:
        if symbol == scope.symbol:
            return Blocked("execution_unknown", (("basis_quarantine", str(quarantine_id)),))
    exposure = dict(values.cells).get(scope.cell_id, _ZERO)
    accounted = (
        accepted.reflected_attempts | accepted.settled_attempts
        | frozenset(attempt_id for attempt_id, _ in accepted.unresolved_attempts)
    )
    pending = _ZERO
    seen: set[UUID] = set()
    for attempt in attempts:
        evidence = (("attempt", str(attempt.attempt_id)),)
        if (attempt.scope.account_id, attempt.scope.environment) != identity:
            return Blocked("attempt_intent_scope_conflict", evidence)
        if attempt.attempt_id in seen:
            return Blocked("duplicate_attempt_intent", evidence)
        seen.add(attempt.attempt_id)
        if attempt.attempt_id in accounted:
            continue
        if attempt.attempt_seq <= accepted.attempt_seq_high_water:
            return Blocked("unclassifiable_commitment", evidence)
        if attempt.scope.symbol != scope.symbol:
            continue
        if attempt.outcome == "unknown" and attempt.resolution is None:
            return Blocked("execution_unknown", evidence)
        if attempt.outcome in {"rejected", "not_sent"} or attempt.resolution == "not_sent":
            continue
        pending += attempt.amount
        if attempt.scope.cell_id == scope.cell_id:
            exposure += attempt.amount
    snapshot = CapitalSnapshot(
        values.available, pending, values.available + values.offered + values.credits, exposure,
    )
    budget = evaluate_capital(policy.policy, snapshot)
    return Available(CapitalView(
        policy, accepted.query_id, accepted.observation_id, snapshot, budget,
        values.unattributed_credits, accepted,
    ))
