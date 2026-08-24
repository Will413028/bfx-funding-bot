from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

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
    assert result.decision.offer_rate == 0.00021
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


@dataclass(frozen=True)
class _FillModelEvidence:
    model_version: str
    artifact_hash: str
    fill_prob: float
    expected_ttf_ms: int
    n_samples: int
    low_confidence: bool = False


@dataclass(frozen=True)
class _FillModelUnavailable:
    reason: str


@dataclass(frozen=True)
class _BoolStyleLowConfidence:
    low_confidence: bool


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
        fill_evidence=_FillModelEvidence(
            model_version="fill-v1",
            artifact_hash="abc123",
            fill_prob=0.8,
            expected_ttf_ms=30_000,
            n_samples=200,
        ),
        safety=GuardResult(True, "risk"),
        audit_context=_context(candidate),
    )

    assert isinstance(result, ReadyToSubmit)
    assert result.model_version == "fill-v1"


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
