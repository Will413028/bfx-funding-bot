from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.audit.model import AuditContext
from bfx_funding_bot.modules.execution.audit.recorder import ExecutionAuditUnavailable
from bfx_funding_bot.modules.execution.contracts import (
    BlockedExecution,
    BlockReason,
    DecisionOutcome,
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.deployment import eligibility
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.deployment.period_pricing import (
    PriceBranch,
    PriceDecision,
)
from bfx_funding_bot.modules.lending.tracking.artifact import (
    FillModelEvidence,
    FillModelUnavailable,
)
from bfx_funding_bot.modules.marketfeed.funding_book import MarketSnapshot
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome as PayloadOutcome
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload


class _Audit:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.last = None

    async def record(self, decision) -> None:
        if self.unavailable:
            raise ExecutionAuditUnavailable("database offline")
        self.last = decision


class _Readiness:
    def __init__(self) -> None:
        self.ready = False
        self.blocked: tuple[BlockReason, str] | None = None

    def set_ready(self) -> None:
        self.ready = True

    def set_blocked(self, reason: BlockReason, dependency: str) -> None:
        self.blocked = (reason, dependency)


class _ExecutionEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    async def emit_execution_event(self, event_name: str, **kwargs: object) -> None:
        self.events.append((event_name, kwargs))


class _ExecutionMetrics:
    def __init__(self) -> None:
        self.decisions: list[dict[str, str]] = []

    def observe_execution_decision(self, **kwargs: str) -> None:
        self.decisions.append(kwargs)

    def observe_audit_persist_failure(self) -> None:
        pass

    def observe_execution_gate_duration(self, *, seconds: float) -> None:
        pass


def _candidate() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=PayloadOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.00020,
        offer_amount_usdt=100.0,
        offer_duration_days=14,
        symbol="fUST",
    )


def _context(candidate: DecisionPayload) -> AuditContext:
    return AuditContext(
        account_id="acct",
        deployment_environment="test",
        reconcile_id="reconcile-1",
        cell_id="cell-1",
        symbol=candidate.symbol,
        signal_correlation_id=str(candidate.signal_correlation_id),
        service_version="test",
        config_hash="config",
    )


def _price() -> PriceDecision:
    return PriceDecision(
        rate=Decimal("0.00021"),
        branch=PriceBranch.UNDERCUT,
        evidence={"period_days": 14},
    )


def _snapshot(
    *,
    symbol: str = "fUST",
    sequence_valid: bool = True,
    checksum_valid: bool = True,
) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id="book-1",
        symbol=symbol,
        bids=(),
        asks=(),
        captured_at_ms=1_000,
        received_at_ms=1_000,
        source="ws",
        sequence_valid=sequence_valid,
        checksum_valid=checksum_valid,
        sequence=2,
    )


async def test_ready_candidate_is_audited_before_becoming_submit_ready() -> None:
    candidate = _candidate()
    audit = _Audit()
    readiness = _Readiness()
    gate = ExecutionGate(
        policy=ExecutionPolicy.BOOK_GUARDED,
        audit=audit,
        readiness=readiness,
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-1",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=None,
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, ReadyToSubmit)
    assert result.decision.offer_rate == Decimal("0.00021")
    assert audit.last.outcome is DecisionOutcome.READY
    assert audit.last.signal_rate == Decimal("0.00020")
    assert audit.last.applied_rate == Decimal("0.00021")
    assert readiness.ready is True


async def test_safety_block_is_audited_with_stable_reason() -> None:
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.BOOK_GUARDED,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-2",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=None,
        safety=GuardResult(False, "risk", "limit"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.SAFETY_GUARD_BLOCKED
    assert audit.last.outcome is DecisionOutcome.BLOCKED
    assert audit.last.reason_code is BlockReason.SAFETY_GUARD_BLOCKED


async def test_blocked_candidate_emits_bounded_execution_events_and_metrics() -> None:
    candidate = _candidate()
    events = _ExecutionEvents()
    metrics = _ExecutionMetrics()
    gate = ExecutionGate(
        policy=ExecutionPolicy.BOOK_GUARDED,
        audit=_Audit(),
        readiness=_Readiness(),
        events=events,
        metrics=metrics,
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-observed",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=None,
        safety=GuardResult(False, "risk", "limit"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert [name for name, _ in events.events] == [
        "funding.execution.eligibility", "funding.execution.blocked",
    ]
    assert metrics.decisions == [{
        "outcome": "blocked",
        "reason": "safety_guard_blocked",
        "policy": "book_guarded",
    }]


async def test_audit_failure_blocks_and_updates_readiness_without_ready_value() -> None:
    candidate = _candidate()
    readiness = _Readiness()
    gate = ExecutionGate(
        policy=ExecutionPolicy.BOOK_GUARDED,
        audit=_Audit(unavailable=True),
        readiness=readiness,
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-3",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=None,
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.EXECUTION_AUDIT_UNAVAILABLE
    assert readiness.blocked == (BlockReason.EXECUTION_AUDIT_UNAVAILABLE, "audit")


async def test_audit_failure_does_not_construct_ready_to_submit(
    monkeypatch,
) -> None:
    candidate = _candidate()
    constructed = False

    class _ReadySpy:
        def __init__(self, **_kwargs) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(eligibility, "ReadyToSubmit", _ReadySpy)
    gate = ExecutionGate(
        policy=ExecutionPolicy.BOOK_GUARDED,
        audit=_Audit(unavailable=True),
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-3a",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=None,
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.EXECUTION_AUDIT_UNAVAILABLE
    assert constructed is False


async def test_other_symbol_snapshot_blocks_before_ready_with_symbol_evidence() -> None:
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.BOOK_GUARDED,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-symbol",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(symbol="fUSD"),
        price=_price(),
        fill_evidence=None,
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.BOOK_STALE
    assert result.failed_dependency == "market_snapshot_symbol"
    assert result.evidence["expected_symbol"] == "fUST"
    assert result.evidence["actual_symbol"] == "fUSD"
    assert audit.last.outcome is DecisionOutcome.BLOCKED


async def test_optimizer_live_without_fill_evidence_is_blocked_and_audited() -> None:
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-4",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=None,
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.FILL_MODEL_MISSING
    assert audit.last.reason_code is BlockReason.FILL_MODEL_MISSING


async def test_optimizer_live_typed_missing_model_evidence_is_blocked_and_audited() -> None:
    """The optimizer-live gate must not convert a model outage into a signal fallback."""
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-model-missing",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=FillModelUnavailable("missing"),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.FILL_MODEL_MISSING
    assert audit.last.reason_code is BlockReason.FILL_MODEL_MISSING


async def test_optimizer_shadow_keeps_book_guarded_rate_when_model_is_unavailable() -> None:
    """Shadow telemetry is observational and cannot change the submitted price."""
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_SHADOW,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-shadow-model-missing",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=FillModelUnavailable("missing"),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, ReadyToSubmit)
    assert result.decision.offer_rate == Decimal("0.00021")
    assert audit.last.model_evidence == {"unavailable_reason": "fill_model_missing"}


@pytest.mark.parametrize(
    ("expected_period_agg", "expected_horizon_h"),
    [("a30", 1), ("p2", 2)],
)
async def test_optimizer_live_scope_mismatch_is_fail_closed(
    expected_period_agg: str,
    expected_horizon_h: int,
) -> None:
    """A model from another exact period or horizon cannot become ReadyToSubmit."""
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-scope-mismatch",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=FillModelEvidence(
            fill_prob=Decimal("0.8"),
            expected_ttf_ms=30_000,
            n_samples=200,
            symbol="fUST",
            period_agg="p2",
            horizon_h=1,
            model_version="fill-v1",
            artifact_hash="abc123",
            cutoff_ms=1_000,
        ),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
        expected_period_agg=expected_period_agg,
        expected_horizon_h=expected_horizon_h,
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason.value == "fill_model_scope_mismatch"
    assert audit.last.reason_code.value == "fill_model_scope_mismatch"


async def test_optimizer_live_accepts_canonical_task7_fill_evidence() -> None:
    candidate = _candidate()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=_Audit(),
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-canonical-evidence",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=FillModelEvidence(
            fill_prob=Decimal("0.8"),
            expected_ttf_ms=30_000,
            n_samples=200,
            symbol="fUST",
            period_agg="p2",
            horizon_h=1,
            model_version="fill-v1",
            artifact_hash="abc123",
            cutoff_ms=1_000,
        ),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
        expected_period_agg="p2",
        expected_horizon_h=1,
    )

    assert isinstance(result, ReadyToSubmit)
    assert result.model_version == "fill-v1"


@pytest.mark.parametrize("reason", ["scope_mismatch", "unversioned"])
async def test_typed_unavailable_reason_is_preserved_for_live_and_audit(
    reason: str,
) -> None:
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id=f"decision-{reason}",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=FillModelUnavailable(reason=reason),  # type: ignore[arg-type]
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason.value == f"fill_model_{reason}"
    assert audit.last.model_evidence == {"unavailable_reason": f"fill_model_{reason}"}


async def test_shadow_preserves_typed_unavailable_reason_without_blocking() -> None:
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_SHADOW,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-shadow-scope-mismatch",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=FillModelUnavailable(reason="scope_mismatch"),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, ReadyToSubmit)
    assert audit.last.model_evidence == {
        "unavailable_reason": "fill_model_scope_mismatch",
    }


@dataclass(frozen=True)
class _FillModelEvidence:
    model_version: str
    artifact_hash: str
    fill_prob: float
    expected_ttf_ms: int
    n_samples: int
    symbol: str
    period_agg: str
    horizon_h: int
    cutoff_ms: int


@dataclass(frozen=True)
class _FillModelUnavailable:
    reason: str


@dataclass(frozen=True)
class _BoolStyleLowConfidence:
    low_confidence: bool


def _fill_evidence(**overrides: object) -> _FillModelEvidence:
    values: dict[str, object] = {
        "model_version": "fill-v1",
        "artifact_hash": "abc123",
        "fill_prob": 0.8,
        "expected_ttf_ms": 30_000,
        "n_samples": 200,
        "symbol": "fUST",
        "period_agg": "a30",
        "horizon_h": 1,
        "cutoff_ms": 1_000,
    }
    values.update(overrides)
    return _FillModelEvidence(**values)


async def test_optimizer_live_accepts_only_structural_fill_model_evidence() -> None:
    candidate = _candidate()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=_Audit(),
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-evidence",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=_fill_evidence(),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, ReadyToSubmit)
    assert result.model_version == "fill-v1"


async def test_optimizer_live_other_symbol_fill_evidence_is_blocked() -> None:
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-evidence-symbol",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=_fill_evidence(symbol="fUSD"),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.FILL_MODEL_MISSING
    assert result.failed_dependency == "fill_model"
    assert audit.last.reason_code is BlockReason.FILL_MODEL_MISSING


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fill_prob", float("nan")),
        ("fill_prob", float("inf")),
        ("fill_prob", -0.01),
        ("fill_prob", 1.01),
        ("n_samples", -1),
        ("expected_ttf_ms", -1),
        ("horizon_h", 0),
        ("cutoff_ms", -1),
    ],
)
async def test_optimizer_live_invalid_fill_evidence_numbers_are_blocked(
    field: str,
    value: object,
) -> None:
    candidate = _candidate()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=_Audit(),
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id=f"decision-invalid-{field}",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=_fill_evidence(**{field: value}),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.FILL_MODEL_MISSING
    assert result.failed_dependency == "fill_model"


async def test_optimizer_live_low_confidence_unavailable_evidence_is_blocked() -> None:
    candidate = _candidate()
    audit = _Audit()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=audit,
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-low-confidence",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=_FillModelUnavailable(reason="low_confidence"),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.FILL_MODEL_LOW_CONFIDENCE
    assert audit.last.reason_code is BlockReason.FILL_MODEL_LOW_CONFIDENCE


async def test_optimizer_live_bool_style_low_confidence_is_blocked() -> None:
    candidate = _candidate()
    gate = ExecutionGate(
        policy=ExecutionPolicy.OPTIMIZER_LIVE,
        audit=_Audit(),
        readiness=_Readiness(),
    )

    result = await gate.prepare(
        candidate,
        decision_id="decision-bool-low-confidence",
        reconcile_id="reconcile-1",
        snapshot=_snapshot(),
        price=_price(),
        fill_evidence=_BoolStyleLowConfidence(low_confidence=True),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.FILL_MODEL_LOW_CONFIDENCE
