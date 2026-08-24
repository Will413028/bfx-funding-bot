import logging
from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.contracts import (
    BlockReason,
    ExecutionPolicy,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.contracts import (
    DecisionOutcome as ExecutionDecisionOutcome,
)
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.deployment.ladder import LadderPolicy
from bfx_funding_bot.modules.execution.deployment.period_pricing import PeriodPricer
from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
from bfx_funding_bot.modules.execution.deployment.reprice import RepricePolicy
from bfx_funding_bot.modules.execution.deployment.standing_quote import (
    StandingQuote,
    StandingQuoteStore,
)
from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    GuardResult,
    SubmittedOrder,
)
from bfx_funding_bot.modules.lending.tracking.artifact import (
    FillModelEvidence,
    FillModelUnavailable,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.funding_book import MarketSnapshot
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    EventType,
    Phase,
    StrategyName,
)

D = Decimal


class _CapturingSink:
    """Captures structured events emitted to the operational stdout port."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.execution_events: list[tuple[str, dict[str, object]]] = []

    async def emit(self, event: dict) -> None:
        self.events.append(event)

    async def emit_execution_event(self, event_name: str, **kwargs: object) -> None:
        self.execution_events.append((event_name, kwargs))


class _FakeLedger:
    def __init__(
        self, exposure: Decimal, reserved: Decimal | None = None,
        available: Decimal | None = None,
        available_by_symbol: dict[str, Decimal] | None = None,
        exposures: dict[str, Decimal] | None = None,
        reserved_by_symbol: dict[str, Decimal] | None = None,
    ) -> None:
        self._e = exposure
        # Default: reserved == exposure (all capital is reserved / open offers).
        # Pass reserved explicitly when simulating realized-only or mixed scenarios.
        self._reserved = reserved if reserved is not None else exposure
        # Default: effectively unbounded so existing cap-driven tests are unaffected.
        self._available = available if available is not None else Decimal("1000000")
        self._available_by_symbol = available_by_symbol or {}
        # Phase 2 multi-symbol: per-symbol exposure / reserved buckets. When a
        # symbol is absent these fall back to the scalar (single-symbol parity).
        self._exposures = exposures or {}
        self._reserved_by_symbol = reserved_by_symbol or {}

    def current_exposure(self, symbol: str) -> Decimal:
        if symbol in self._exposures:
            return self._exposures[symbol]
        return self._e

    def reserved_exposure(self, symbol: str) -> Decimal:
        if symbol in self._reserved_by_symbol:
            return self._reserved_by_symbol[symbol]
        # Default reserved tracks per-symbol exposure when only exposures given.
        if symbol in self._exposures:
            return self._exposures[symbol]
        return self._reserved

    def available_balance(self, symbol: str) -> Decimal:
        if symbol in self._available_by_symbol:
            return self._available_by_symbol[symbol]
        return self._available


class _FakeSafety:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list = []

    async def evaluate(self, decision, ctx) -> GuardResult:
        self.calls.append(decision)
        return GuardResult(
            allowed=self.allowed, guard_name="fake",
            reason=None if self.allowed else "blocked",
        )


class _FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list = []
        self.ready_submissions: list[ReadyToSubmit] = []

    async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
        self.ready_submissions.append(decision)
        self.submitted.append(decision.decision)
        return SubmittedOrder(cid=1, venue_offer_id="x", status="submitted", raw_response=None)


class _Audit:
    def __init__(self) -> None:
        self.last = None

    async def record(self, decision) -> None:
        self.last = decision


class _Readiness:
    def set_ready(self) -> None:
        pass

    def set_blocked(self, reason, dependency) -> None:
        pass


class _SnapshotProvider:
    def __init__(self, snapshot: MarketSnapshot | None) -> None:
        self._snapshot = snapshot

    def snapshot(self, symbol: str, *, now_ms: int) -> MarketSnapshot | None:
        return self._snapshot


class _FillModelProvider:
    def __init__(self, evidence: FillModelEvidence | FillModelUnavailable | None) -> None:
        self._evidence = evidence

    def estimate_fill(
        self,
        reference_rate: Decimal,
        offer_rate: Decimal,
        period_agg: str,
        horizon_h: int,
    ) -> FillModelEvidence | FillModelUnavailable | None:
        return self._evidence


class _AuditContexts:
    def build(self, *, candidate, cell_id: str, reconcile_id: str):
        from bfx_funding_bot.modules.execution.audit.model import AuditContext

        return AuditContext(
            account_id="default", deployment_environment="test", reconcile_id=reconcile_id,
            cell_id=cell_id, symbol=candidate.symbol,
            signal_correlation_id=str(candidate.signal_correlation_id),
            service_version="test", config_hash="test",
        )


def _valid_snapshot() -> MarketSnapshot:
    from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel

    return MarketSnapshot(
        snapshot_id="book-1", symbol="fUST",
        bids=(FundingBookLevel(rate=0.001, period=2, count=1, amount=-100_000),),
        asks=(), captured_at_ms=1_000, received_at_ms=1_000, source="ws",
        sequence_valid=True, checksum_valid=True, sequence=2,
    )


def _ask_snapshot() -> MarketSnapshot:
    from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel

    return MarketSnapshot(
        snapshot_id="book-ask", symbol="fUST", bids=(),
        asks=(FundingBookLevel(rate=0.00025, period=2, count=1, amount=100_000),),
        captured_at_ms=1_000, received_at_ms=1_000, source="ws",
        sequence_valid=True, checksum_valid=True, sequence=2,
    )


def _signal_floor_snapshot() -> MarketSnapshot:
    from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel

    return MarketSnapshot(
        snapshot_id="book-floor", symbol="fUST", bids=(),
        asks=(FundingBookLevel(rate=0.0001, period=2, count=1, amount=100_000),),
        captured_at_ms=1_000, received_at_ms=1_000, source="ws",
        sequence_valid=True, checksum_valid=True, sequence=2,
    )


def _raise_snapshot() -> MarketSnapshot:
    from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel

    return MarketSnapshot(
        snapshot_id="book-raise", symbol="fUST", bids=(),
        asks=(FundingBookLevel(rate=0.00012, period=2, count=1, amount=100_000),),
        captured_at_ms=1_000, received_at_ms=1_000, source="ws",
        sequence_valid=True, checksum_valid=True, sequence=2,
    )


def _eligibility_kwargs(
    *, audit=None, book_provider=None,
    gate_policy: ExecutionPolicy = ExecutionPolicy.BOOK_GUARDED,
    execution_policy: ExecutionPolicy = ExecutionPolicy.BOOK_GUARDED,
    fill_model_provider: _FillModelProvider | None = None,
) -> dict:
    return {
        "book_provider": book_provider or _SnapshotProvider(_valid_snapshot()),
        "execution_gate": ExecutionGate(
            policy=gate_policy,
            audit=audit or _Audit(), readiness=_Readiness(),
        ),
        "execution_policy": execution_policy,
        "period_pricer": PeriodPricer(
            max_down_pct=D("0.15"), tick=D("0.00000001"),
        ),
        "audit_context_factory": _AuditContexts(),
        "fill_model_provider": fill_model_provider,
    }


def _cell(symbol: str, period_agg: str) -> CellConfig:
    return CellConfig(
        strategy=StrategyName.MEAN_REVERSION, symbol=symbol, period_agg=period_agg,
        timeframe="1h",
        params={"threshold_sigma": 1.0, "ratio_sigma": 1.0, "ema_span": 10},
        reference_amount_usdt=150.0,
        staleness_budget_hours=48,
    )


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=D("570"),
    )


def _post_quote(cell_id: str) -> StandingQuote:
    return StandingQuote(
        cell_id=cell_id, outcome=DecisionOutcome.POST, rate=0.00012,
        period_days=2, signal_correlation_id=uuid4(), created_at_ms=1_000,
    )


class _SeqSafety:
    """Safety fake whose allow/deny verdict is controlled per-call by a sequence."""

    def __init__(self, allowed_seq: list[bool]) -> None:
        self._seq = list(allowed_seq)
        self.calls: list = []

    async def evaluate(self, decision, ctx) -> GuardResult:
        self.calls.append(decision)
        allowed = self._seq.pop(0) if self._seq else True
        return GuardResult(
            allowed=allowed, guard_name="seq",
            reason=None if allowed else "blocked",
        )


def _build(*, exposure, quotes, safety_allowed=True, executor=None, safety=None,
           available=None, event_sink=None, canceller=None, reprice=None,
           ladder=None, cap=None,
           attempt_recorder=None, book_provider=None, audit=None,
           gate_policy=ExecutionPolicy.BOOK_GUARDED,
           execution_policy=ExecutionPolicy.BOOK_GUARDED,
           fill_model_provider: _FillModelProvider | None = None,
           optimizer_fee_rate: Decimal | None = None,
           optimizer_horizon_h: int | None = None):
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    for q in quotes:
        store.update(q)
    tracker = CellDeploymentTracker()
    ex = executor or _FakeExecutor()
    safety = safety if safety is not None else _FakeSafety(allowed=safety_allowed)
    # cap override: only the ladder observe-log test needs a gap large enough
    # (>= min_rung_usdt / spike_fraction) to actually produce spike rungs; every
    # other caller keeps the default _ctx() (allocation_cap_usdt=570).
    ctx = _ctx() if cap is None else AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=cap,
    )
    optimizer_kwargs: dict[str, object] = {}
    if optimizer_horizon_h is not None:
        optimizer_kwargs["optimizer_horizon_h"] = optimizer_horizon_h
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=_FakeLedger(exposure, available=available),
        safety_chain=safety, executor=ex, account_ctx=ctx, cells=cells,
        venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), balance_buffer_usdt=D("3"),
        clock=lambda: 1_000,
        event_sink=event_sink if event_sink is not None else _CapturingSink(),
        phase=Phase.CANARY,
        canceller=canceller,
        reprice=reprice,
        **_eligibility_kwargs(
            audit=audit,
            book_provider=book_provider,
            gate_policy=gate_policy,
            execution_policy=execution_policy,
            fill_model_provider=fill_model_provider,
        ),
        ladder=ladder,
        attempt_recorder=attempt_recorder,
        optimizer_fee_rate=optimizer_fee_rate,
        **optimizer_kwargs,
    )
    return rec, ex, tracker, safety


def _fill_evidence() -> FillModelEvidence:
    return FillModelEvidence(
        model_version="fill-v1",
        artifact_hash="sha256:model",
        fill_prob=D("0.95"),
        expected_ttf_ms=500,
        n_samples=100,
        symbol="fUST",
        period_agg="p2",
        horizon_h=1,
        cutoff_ms=1_000,
    )


def test_reconciler_rejects_mismatched_execution_gate_policy() -> None:
    with pytest.raises(ValueError, match="execution_gate policy"):
        _build(
            exposure=D("370"),
            quotes=[_post_quote("fUST_a30")],
            execution_policy=ExecutionPolicy.PAPER,
        )


async def test_book_failure_never_submits_original_quote():
    audit = _Audit()
    rec, executor, _tracker, _safety = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        book_provider=_SnapshotProvider(None), audit=audit,
    )

    await rec.deploy()

    assert executor.submitted == []
    assert audit.last.outcome is ExecutionDecisionOutcome.BLOCKED
    assert audit.last.reason_code is BlockReason.BOOK_STALE


async def test_reconciler_releases_only_audited_ready_to_executor_and_event():
    audit = _Audit()
    sink = _CapturingSink()

    class _AuditAwareExecutor(_FakeExecutor):
        async def submit(self, ready, ctx, *, cid=None) -> SubmittedOrder:
            assert isinstance(ready, ReadyToSubmit)
            assert audit.last is not None
            assert audit.last.outcome is ExecutionDecisionOutcome.READY
            return await super().submit(ready, ctx, cid=cid)

    executor = _AuditAwareExecutor()
    rec, _executor, _tracker, _safety = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        executor=executor, event_sink=sink, audit=audit,
    )

    await rec.deploy()

    assert len(executor.ready_submissions) == 1
    ready = executor.ready_submissions[0]
    assert ready.decision_id == audit.last.decision_id
    order_submit = next(event for event in sink.events if event["event_type"] == "order_submit")
    assert order_submit["payload"]["execution_decision_id"] == ready.decision_id


async def test_optimizer_shadow_records_unavailable_model_without_blocking_book_guarded_submit():
    """Shadow model failure is observable but cannot change deploy eligibility or rate."""
    audit = _Audit()
    sink = _CapturingSink()
    rec, executor, _tracker, _safety = _build(
        exposure=D("370"),
        quotes=[_post_quote("fUST_p2")],
        book_provider=_SnapshotProvider(_ask_snapshot()),
        audit=audit,
        event_sink=sink,
        gate_policy=ExecutionPolicy.OPTIMIZER_SHADOW,
        execution_policy=ExecutionPolicy.OPTIMIZER_SHADOW,
        fill_model_provider=_FillModelProvider(FillModelUnavailable("missing")),
    )

    await rec.deploy()

    assert [decision.offer_rate for decision in executor.submitted] == [0.00024999]
    assert audit.last.model_evidence["unavailable_reason"] == "fill_model_missing"
    assert [name for name, _ in sink.execution_events] == [
        "funding.fill_model.unavailable",
        "funding.optimizer.no_recommendation",
        "funding.execution.submitted",
    ]


@pytest.mark.parametrize("unavailable_reason", [
    "low_confidence", "scope_mismatch", "unversioned",
])
async def test_optimizer_shadow_emits_unavailable_event_for_canonical_reason(
    unavailable_reason: str,
) -> None:
    audit = _Audit()
    sink = _CapturingSink()
    rec, executor, _tracker, _safety = _build(
        exposure=D("370"),
        quotes=[_post_quote("fUST_p2")],
        book_provider=_SnapshotProvider(_ask_snapshot()),
        audit=audit,
        event_sink=sink,
        gate_policy=ExecutionPolicy.OPTIMIZER_SHADOW,
        execution_policy=ExecutionPolicy.OPTIMIZER_SHADOW,
        fill_model_provider=_FillModelProvider(
            FillModelUnavailable(reason=unavailable_reason),  # type: ignore[arg-type]
        ),
    )

    await rec.deploy()

    assert [decision.offer_rate for decision in executor.submitted] == [0.00024999]
    assert [name for name, _ in sink.execution_events] == [
        "funding.fill_model.unavailable",
        "funding.optimizer.no_recommendation",
        "funding.execution.submitted",
    ]


async def test_optimizer_live_uses_selected_exact_period_rate_only_after_audit():
    """Live optimizer selection remains behind the existing audit-before-submit gate."""
    audit = _Audit()
    rec, executor, _tracker, _safety = _build(
        exposure=D("370"),
        quotes=[_post_quote("fUST_p2")],
        book_provider=_SnapshotProvider(_ask_snapshot()),
        audit=audit,
        gate_policy=ExecutionPolicy.OPTIMIZER_LIVE,
        execution_policy=ExecutionPolicy.OPTIMIZER_LIVE,
        fill_model_provider=_FillModelProvider(_fill_evidence()),
        optimizer_fee_rate=D("0.15"),
        optimizer_horizon_h=1,
    )

    await rec.deploy()

    assert [decision.offer_rate for decision in executor.submitted] == [0.00024999]
    assert audit.last.outcome is ExecutionDecisionOutcome.READY
    assert audit.last.applied_rate == D("0.00024999")
    assert audit.last.model_evidence["optimizer"]["selected_source"] == "maker"


async def test_optimizer_live_requires_explicit_fee_rate() -> None:
    with pytest.raises(ValueError, match="fee"):
        _build(
            exposure=D("370"),
            quotes=[_post_quote("fUST_p2")],
            book_provider=_SnapshotProvider(_ask_snapshot()),
            gate_policy=ExecutionPolicy.OPTIMIZER_LIVE,
            execution_policy=ExecutionPolicy.OPTIMIZER_LIVE,
            fill_model_provider=_FillModelProvider(_fill_evidence()),
        )


@pytest.mark.parametrize(
    "snapshot", [_raise_snapshot(), _signal_floor_snapshot()],
    ids=["raise", "signal_floor"],
)
async def test_optimizer_live_does_not_fabricate_maker_for_signal_semantics(
    snapshot: MarketSnapshot,
) -> None:
    """Raise and signal-floor pricing have no exact-period maker candidate."""
    audit = _Audit()
    rec, executor, _tracker, _safety = _build(
        exposure=D("370"),
        quotes=[_post_quote("fUST_p2")],
        book_provider=_SnapshotProvider(snapshot),
        audit=audit,
        gate_policy=ExecutionPolicy.OPTIMIZER_LIVE,
        execution_policy=ExecutionPolicy.OPTIMIZER_LIVE,
        fill_model_provider=_FillModelProvider(_fill_evidence()),
        optimizer_fee_rate=D("0.15"),
        optimizer_horizon_h=1,
    )

    await rec.deploy()

    assert len(executor.submitted) == 1
    optimizer = audit.last.model_evidence["optimizer"]
    assert optimizer["selected_source"] == "signal"
    assert [candidate["source"] for candidate in optimizer["candidates"]] == ["signal"]


async def test_deploys_gap_to_active_cell():
    rec, ex, tracker, _ = _build(exposure=D("370"), quotes=[_post_quote("fUST_a30")])
    await rec.deploy()
    assert len(ex.submitted) == 1
    d = ex.submitted[0]
    assert d.decision_outcome == DecisionOutcome.POST
    assert d.offer_amount_usdt == 200.0   # gap 200, single active, under cap
    assert d.offer_rate == 0.00012
    assert d.offer_duration_days == 2
    assert tracker.deployed("fUST_a30") == D("200")


async def test_no_active_quote_no_submit():
    rec, ex, _, _ = _build(exposure=D("0"), quotes=[])  # no quotes
    await rec.deploy()
    assert ex.submitted == []


async def test_safety_block_skips_submit():
    rec, ex, tracker, safety = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], safety_allowed=False,
    )
    await rec.deploy()
    assert ex.submitted == []
    assert len(safety.calls) == 1            # guard was consulted
    assert tracker.deployed("fUST_a30") == D("0")  # not recorded on block


async def test_full_gap_no_action():
    rec, ex, _, _ = _build(exposure=D("570"), quotes=[_post_quote("fUST_a30")])
    await rec.deploy()
    assert ex.submitted == []


async def test_submit_failure_does_not_record_intent():
    class _Boom(_FakeExecutor):
        async def submit(self, decision, ctx, *, cid=None):
            raise RuntimeError("venue 500")

    rec, _ex, tracker, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], executor=_Boom(),
    )
    await rec.deploy()  # must not raise
    assert tracker.deployed("fUST_a30") == D("0")


async def test_two_active_cells_split_when_gap_exceeds_cap():
    # gap=570, cap_per_cell=0.70*570=399; greedy emptiest-first (tiebreak cell_id):
    # a30 -> 399 (hits cap), p2 -> 171 (remainder)
    rec, ex, tracker, _ = _build(
        exposure=D("0"),
        quotes=[_post_quote("fUST_a30"), _post_quote("fUST_p2")],
    )
    await rec.deploy()
    assert len(ex.submitted) == 2
    assert tracker.deployed("fUST_a30") == D("399")
    assert tracker.deployed("fUST_p2") == D("171")
    amounts = sorted(d.offer_amount_usdt for d in ex.submitted)
    assert amounts == [171.0, 399.0]


async def test_per_cell_safety_block_does_not_stop_other_cell():
    # gap=570 -> a30=399 (blocked), p2=171 (allowed); exactly one submit
    seq_safety = _SeqSafety([False, True])
    rec, ex, tracker, safety = _build(
        exposure=D("0"),
        quotes=[_post_quote("fUST_a30"), _post_quote("fUST_p2")],
        safety=seq_safety,
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_amount_usdt == 171.0
    assert tracker.deployed("fUST_a30") == D("0")
    assert tracker.deployed("fUST_p2") == D("171")
    assert len(safety.calls) == 2  # both cells consulted


class _RejectingExecutor:
    """Live-executor behaviour on a venue reject (e.g. 10001 not-enough-balance):
    returns a SubmittedOrder with status="failed" rather than raising."""

    def __init__(self) -> None:
        self.submitted: list = []

    async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
        self.submitted.append(decision)
        return SubmittedOrder(cid=1, venue_offer_id=None, status="failed", raw_response=None)


async def test_venue_rejected_submit_not_recorded_as_deployed():
    # status="failed" (venue reject, no exception) must NOT record intent and
    # must NOT count as a deployment_submitted success.
    rec, ex, tracker, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], executor=_RejectingExecutor(),
    )
    await rec.deploy()
    assert len(ex.submitted) == 1            # attempted once
    assert tracker.deployed("fUST_a30") == D("0")  # but not recorded as deployed


# ---------------------------------------------------------------------------
# #5 observability: the live submit path emits structured ORDER_SUBMIT events
# (parity with SIGNAL/DECISION and the paper executor) so a structured-event
# dashboard can see live deploys, not just plain log.info.
# ---------------------------------------------------------------------------


async def test_successful_submit_emits_order_submit_structured_event():
    sink = _CapturingSink()
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], event_sink=sink,
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    submits = [e for e in sink.events
               if e["event_type"] == EventType.ORDER_SUBMIT.value]
    assert len(submits) == 1
    ev = submits[0]
    assert ev["cell"] == "fUST_a30"
    payload = ev["payload"]
    assert payload["is_simulated"] is False  # live deploy, distinguishes from paper
    assert payload["status"] == "submitted"
    assert payload["offer_amount_usdt"] == 200.0
    assert payload["cid"] == 1
    assert payload["offer_id"] == "x"
    assert [name for name, _ in sink.execution_events] == [
        "funding.execution.submitted",
    ]


async def test_venue_rejected_submit_emits_failed_order_submit_event():
    sink = _CapturingSink()
    rec, _ex, tracker, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        executor=_RejectingExecutor(), event_sink=sink,
    )
    await rec.deploy()
    submits = [e for e in sink.events
               if e["event_type"] == EventType.ORDER_SUBMIT.value]
    assert len(submits) == 1
    assert submits[0]["payload"]["status"] == "failed"
    assert submits[0]["payload"]["is_simulated"] is False
    assert tracker.deployed("fUST_a30") == D("0")  # reject still not deployed


# ---------------------------------------------------------------------------
# Balance-aware cap gate: clamp deploy to available − buffer
# ---------------------------------------------------------------------------

async def test_clamps_deploy_to_available_minus_buffer():
    # cap gap = 570 - 406.89 = 163.11; available 150, buffer 3 -> headroom 147
    # < min_fill 153 -> sleep (the incident scenario).
    rec, ex, tracker, _ = _build(
        exposure=D("406.89"), quotes=[_post_quote("fUST_a30")], available=D("150"),
    )
    await rec.deploy()
    assert ex.submitted == []
    assert tracker.deployed("fUST_a30") == D("0")


async def test_deploys_when_available_sufficient():
    # cap gap = 570 - 370 = 200; available 250, buffer 3 -> headroom 247 >= 200
    # -> deploy full cap gap 200.
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], available=D("250"),
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_amount_usdt == 200.0


async def test_available_headroom_binds_below_cap_gap():
    # cap gap = 570 - 200 = 370; available 320, buffer 3 -> headroom 317 -> deploy 317.
    rec, ex, _, _ = _build(
        exposure=D("200"), quotes=[_post_quote("fUST_a30")], available=D("320"),
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_amount_usdt == 317.0


# ---------------------------------------------------------------------------
# Stranded-capital log attribution: balance-limited vs concentration
# ---------------------------------------------------------------------------


async def test_stranded_log_names_balance_limit_when_headroom_binds(caplog):
    # gap = cap - exposure = 570 - 0 = 570; available 203, buffer 3 -> headroom 200.
    # headroom (200) binds below the policy gap (570) but is >= min_fill (153), so a
    # single cell deploys 200 and 370 is stranded. The real cause is insufficient
    # funding-wallet balance, NOT the concentration cap — the log must say so.
    rec, _ex, _, _ = _build(
        exposure=D("0"), quotes=[_post_quote("fUST_a30")], available=D("203"),
    )
    with caplog.at_level(logging.INFO):
        await rec.deploy()
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "deployment_capital_stranded" in msg
    assert "balance-limited" in msg
    assert "concentration cap" not in msg


async def test_stranded_log_names_concentration_when_balance_ample(caplog):
    # Single-active-cell relaxation raises cap_per_cell to the full policy
    # target (max(0.70*cap, cap/1) = cap) — a lone cell starting from empty
    # can no longer be concentration-capped-stranded (see Task 2). Genuine
    # concentration/no-further-active-cell stranding still happens when the
    # lone active cell already carries deployed intent close to that (now
    # relaxed) cap: cap=10000, cell already at 9500 (== reserved, so the
    # tracker's reconcile_to_total rescale is a no-op) -> cap_per_cell=10000,
    # e_total=8000 -> gap=2000, cell headroom=10000-9500=500 -> allocated=500,
    # stranded=1500 (>= min_fill, no other active cell to absorb it), while
    # balance is ample.
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post_quote("fUST_a30"))  # only "a30" active; "p2" idle
    tracker = CellDeploymentTracker()
    tracker.record_deploy("fUST_a30", D("9500"))
    ledger = _FakeLedger(
        exposure=D("8000"), reserved=D("9500"), available=D("1000000"),
    )
    ctx = AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=D("10000"),
    )
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=ledger,
        safety_chain=_FakeSafety(allowed=True), executor=_FakeExecutor(),
        account_ctx=ctx, cells=cells, venue_floor_usd=D("150"),
        min_offer_buffer_pct=D("0.02"), concentration_pct=D("0.70"),
        balance_buffer_usdt=D("3"), clock=lambda: 1_000,
        event_sink=_CapturingSink(), phase=Phase.CANARY,
        **_eligibility_kwargs(),
    )
    with caplog.at_level(logging.INFO):
        await rec.deploy()
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "deployment_capital_stranded" in msg
    assert "concentration cap" in msg
    assert "balance-limited" not in msg


# ---------------------------------------------------------------------------
# C1 regression: orphan realized credits must not inflate/starve cells
# ---------------------------------------------------------------------------

def _build_with_split_ledger(*, reserved, realized, quotes):
    """Build reconciler with explicit reserved / realized split."""
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    for q in quotes:
        store.update(q)
    tracker = CellDeploymentTracker()
    exposure = reserved + realized
    ledger = _FakeLedger(exposure=exposure, reserved=reserved)
    ex = _FakeExecutor()
    safety = _FakeSafety(allowed=True)
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=ledger,
        safety_chain=safety, executor=ex, account_ctx=_ctx(), cells=cells,
        venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), balance_buffer_usdt=D("3"),
        clock=lambda: 1_000,
        event_sink=_CapturingSink(), phase=Phase.CANARY,
        **_eligibility_kwargs(),
    )
    return rec, ex, tracker, safety


async def test_orphan_realized_credits_do_not_starve_cell():
    """C1 regression: ledger reserved=$100 (one open offer), realized=$300 (orphan).
    total_exposure=$400 — but reconcile_to_total must use reserved=$100, not $400.
    The cell has recorded intent=$100; factor should be 1.0, NOT 4.0.
    The gap = 570-400 = 170 → the cell is NOT starved; it should get ~170 allocated.
    """
    # Pre-seed the tracker with the open offer we own
    rec, ex, tracker, _ = _build_with_split_ledger(
        reserved=D("100"), realized=D("300"),
        quotes=[_post_quote("fUST_a30")],
    )
    # Simulate the tracker already recorded our $100 open offer
    tracker.record_deploy("fUST_a30", D("100"))
    await rec.deploy()
    # gap = 570 - 400 = 170; cap_per_cell = 0.70*570 = 399; cell has 100 deployed
    # headroom = min(399, 570) - 100 = 299 >= 170 -> fills 170
    assert len(ex.submitted) == 1, "cell should NOT be starved by orphan realized credits"
    assert ex.submitted[0].offer_amount_usdt == pytest.approx(170.0)
    # tracker recorded the new submit on top of the existing 100
    assert tracker.deployed("fUST_a30") == D("270")


# ---------------------------------------------------------------------------
# Cluster D: decision carries the cell symbol; headroom is per-symbol
# ---------------------------------------------------------------------------


async def test_decision_carries_cell_symbol():
    safety = _FakeSafety(allowed=True)
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], safety=safety,
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    # both the decision handed to safety AND to the executor carry the symbol
    assert safety.calls[0].symbol == "fUST"
    assert ex.submitted[0].symbol == "fUST"


async def test_headroom_uses_cell_symbol_available():
    # cap gap = 570 - 370 = 200. The cell symbol fUST has available 250 (buffer 3
    # → headroom 247 >= 200), while the global default is starved (0). Reading the
    # per-symbol balance is what lets the deploy proceed.
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post_quote("fUST_a30"))
    tracker = CellDeploymentTracker()
    ledger = _FakeLedger(
        exposure=D("370"), available=D("0"),
        available_by_symbol={"fUST": D("250")},
    )
    ex = _FakeExecutor()
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=ledger,
        safety_chain=_FakeSafety(allowed=True), executor=ex, account_ctx=_ctx(),
        cells=cells, venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), balance_buffer_usdt=D("3"),
        clock=lambda: 1_000, event_sink=_CapturingSink(), phase=Phase.CANARY,
        **_eligibility_kwargs(),
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_amount_usdt == 200.0


# ---------------------------------------------------------------------------
# Phase 2 Task 8: independent per-symbol gap pools (reconciler is the real-money
# sizing authority — each currency is sized against ITS own cap[symbol], and a
# symbol with cap=0 ships dark, producing zero offers).
# ---------------------------------------------------------------------------


def _build_multi(*, cells, exposures, available_by_symbol, caps, buffers,
                 quotes=None, executor=None, safety=None, event_sink=None):
    store = StandingQuoteStore(ttl_ms=3_900_000)
    if quotes is None:
        quotes = [_post_quote(c.cell_id) for c in cells]
    for q in quotes:
        store.update(q)
    tracker = CellDeploymentTracker()
    ex = executor or _FakeExecutor()
    safety = safety if safety is not None else _FakeSafety(allowed=True)
    ledger = _FakeLedger(
        exposure=D("0"),
        exposures=exposures,
        available_by_symbol=available_by_symbol,
    )
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=ledger,
        safety_chain=safety, executor=ex, account_ctx=_ctx(), cells=cells,
        venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), balance_buffer_usdt=D("3"),
        caps=caps, default_cap=D("0"), buffers=buffers, default_buffer=D("0"),
        clock=lambda: 1_000,
        event_sink=event_sink if event_sink is not None else _CapturingSink(),
        phase=Phase.CANARY,
        **_eligibility_kwargs(),
    )
    return rec, ex, tracker, safety


async def test_independent_per_symbol_gap_pools():
    cells = [_cell("fUST", "a30"), _cell("fUSD", "a30")]   # TWO symbols
    rec, ex, _tracker, _ = _build_multi(
        cells=cells,
        exposures={"fUST": D("0"), "fUSD": D("0")},
        available_by_symbol={"fUST": D("5000"), "fUSD": D("5000")},
        caps={"fUST": D("3000"), "fUSD": D("0")},  # fUSD disabled (dark)
        buffers={"fUST": D("3"), "fUSD": D("3")},
    )
    await rec.deploy()
    submitted = ex.submitted
    # fUST sized against its 3000 cap; fUSD cap=0 → skipped, zero offers.
    assert submitted, "expected fUST offers"
    assert all(s.symbol == "fUST" for s in submitted)
    # Lock that caps["fUST"]=3000 (the map) drives sizing, NOT the 570 _ctx() env
    # fallback — the precise global-cap divergence this task closes.
    assert sum(s.offer_amount_usdt for s in submitted) > 570


# ---------------------------------------------------------------------------
# E1: stale-offer reprice sweep (execution layer). Cancel wiring runs BEFORE
# allocate_gap so freed exposure is visible to the reconciler's own reserved
# read next tick (release is reconciled elsewhere — WS foc / next reconcile —
# single-writer ledger; this sweep never touches ledger/tracker/position).
# ---------------------------------------------------------------------------


class _FakeCanceller:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel(self, *, venue_offer_id, signal_correlation_id, account_id, ctx) -> None:
        self.cancelled.append(venue_offer_id)


def _venue_offer(
    voi: str, rate: float, age_ms: int = 3_600_000, symbol: str = "fUST",
) -> ActiveFundingOffer:
    # _build 的 clock 固定回 1_000（ms）；mts_created 允許負值（純 int 運算）
    return ActiveFundingOffer(
        venue_offer_id=voi, symbol=symbol, amount=D("200"), rate=rate,
        period_days=2, mts_created=1_000 - age_ms, status="ACTIVE",
    )


_REPRICE = RepricePolicy(
    enabled=True, tolerance_pct=0.10, min_age_ms=1_800_000, max_cancels_per_tick=3,
)


async def test_sweep_cancels_stale_offer_when_enabled():
    canc = _FakeCanceller()
    rec, _ex, _, _ = _build(
        exposure=D("570"), quotes=[_post_quote("fUST_a30")],
        canceller=canc, reprice=_REPRICE,
    )
    # quote rate 0.00012；offer 0.001 遠超 +10% 且齡 60min
    await rec.deploy(venue_offers=(_venue_offer("42", 0.001),))
    assert canc.cancelled == ["42"]


async def test_sweep_observe_mode_logs_but_does_not_cancel():
    canc = _FakeCanceller()
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        canceller=canc,
        reprice=RepricePolicy(
            enabled=False, tolerance_pct=0.10, min_age_ms=1_800_000,
            max_cancels_per_tick=3,
        ),
    )
    await rec.deploy(venue_offers=(_venue_offer("42", 0.001),))
    assert canc.cancelled == []
    assert len(ex.submitted) == 1  # observe mode 不影響正常部署


async def test_sweep_no_active_quote_no_cancel():
    canc = _FakeCanceller()
    rec, _, _, _ = _build(
        exposure=D("570"), quotes=[], canceller=canc, reprice=_REPRICE,
    )
    await rec.deploy(venue_offers=(_venue_offer("42", 0.001),))
    assert canc.cancelled == []  # SKIP/過期 → resting 高價單留作 spike option


async def test_sweep_respects_per_tick_budget():
    canc = _FakeCanceller()
    rec, _, _, _ = _build(
        exposure=D("570"), quotes=[_post_quote("fUST_a30")],
        canceller=canc,
        reprice=RepricePolicy(
            enabled=True, tolerance_pct=0.10, min_age_ms=1_800_000,
            max_cancels_per_tick=2,
        ),
    )
    offers = tuple(_venue_offer(str(i), 0.001 + i * 0.0001) for i in range(5))
    await rec.deploy(venue_offers=offers)
    assert len(canc.cancelled) == 2
    assert canc.cancelled == ["4", "3"]  # 最超價的先砍


async def test_sweep_cancel_error_does_not_block_deploy():
    class _BoomCanceller(_FakeCanceller):
        async def cancel(self, **kwargs) -> None:
            raise RuntimeError("venue 500")

    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        canceller=_BoomCanceller(), reprice=_REPRICE,
    )
    await rec.deploy(venue_offers=(_venue_offer("42", 0.001),))
    assert len(ex.submitted) == 1  # sweep 失敗不影響 gap 部署


async def test_sweep_auth_error_propagates():
    class _AuthBoom(_FakeCanceller):
        async def cancel(self, **kwargs) -> None:
            raise ExecutorAuthError("digest invalid")

    rec, _, _, _ = _build(
        exposure=D("570"), quotes=[_post_quote("fUST_a30")],
        canceller=_AuthBoom(), reprice=_REPRICE,
    )
    with pytest.raises(ExecutorAuthError):
        await rec.deploy(venue_offers=(_venue_offer("42", 0.001),))


async def test_no_reprice_config_is_noop():
    # reprice=None（預設）→ 與現狀 byte-identical
    canc = _FakeCanceller()
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], canceller=canc,
    )
    await rec.deploy(venue_offers=(_venue_offer("42", 0.001),))
    assert canc.cancelled == []
    assert len(ex.submitted) == 1


# ---------------------------------------------------------------------------
# Single-active-cell stranding fix: the tracker's reconcile_to_total clamp
# must stay aligned with allocate_gap's relaxed per-cell cap.
# ---------------------------------------------------------------------------


async def test_tracker_clamp_uses_relaxed_cap_for_single_active_cell():
    """With 1 active POST quote of 2 configured cells, the tracker rescale clamp
    must use the SAME relaxed cap as allocate_gap — otherwise reconcile_to_total
    clamp-warns every tick once the lone cell's intent legitimately exceeds
    concentration_pct * cap.

    current_exposure == cap (gap=0) isolates the tracker-clamp path from
    allocate_gap: reserved=9000 (the venue-true open-offer total attributable to
    fUST_p2, already recorded in the tracker) is strictly less than exposure
    (9000 reserved + 1000 unattributable realized credit = 10000 = cap), so no
    new fill is computed and reconcile_to_total's clamp is the only thing that
    can move tracker.deployed("fUST_p2").
    """
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post_quote("fUST_p2"))  # only one of the two cells POSTs
    tracker = CellDeploymentTracker()
    tracker.record_deploy("fUST_p2", D("9000"))  # lone cell's venue-true intent
    ledger = _FakeLedger(
        exposure=D("10000"), reserved=D("9000"), available=D("100000"),
    )
    ctx = AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=D("10000"),
    )
    ex = _FakeExecutor()
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=ledger,
        safety_chain=_FakeSafety(allowed=True), executor=ex, account_ctx=ctx,
        cells=cells, venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), balance_buffer_usdt=D("3"),
        clock=lambda: 1_000, event_sink=_CapturingSink(), phase=Phase.CANARY,
        **_eligibility_kwargs(),
    )
    await rec.deploy()
    # NOT clamped down to 7000: relaxed cap = max(7000, 10000/1) = 10000.
    assert tracker.deployed("fUST_p2") == D("9000")


# ---------------------------------------------------------------------------
# Observe-only spike-rung ladder uses the exact-period ask already selected by
# the period pricer; it keeps submit count unchanged.
# ---------------------------------------------------------------------------


_LADDER = LadderPolicy(spike_fraction=0.15, rung_multipliers=(1.5, 3.0), min_rung_usdt=153.0)


async def test_ladder_observe_logs_rungs_without_touching_submits(caplog):
    # cap=10000, single active cell (fUST_p2) -> relaxed cap_per_cell = cap ->
    # full 10000 gap deploys to that one cell. budget = 10000*0.15 = 1500 ->
    # 750/rung >= 153 -> two rungs get logged from the exact-period ask.
    rec, ex, _, _ = _build(
        exposure=D("0"), quotes=[_post_quote("fUST_p2")],
        cap=D("10000"), book_provider=_SnapshotProvider(_ask_snapshot()), ladder=_LADDER,
    )
    with caplog.at_level(logging.INFO):
        await rec.deploy()
    assert any("ladder_would_post" in r.getMessage() for r in caplog.records)
    # observe-only invariant: exactly the same submit as without a ladder —
    # one offer, at the exact-period maker price.
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_rate == 0.00024999


async def test_no_ladder_config_never_computes_rungs(caplog):
    # ladder=None (default) -> byte-identical to pre-Task-6: same submit, no
    # ladder_would_post log line, even with an identical exact-period ask/cap setup.
    rec, ex, _, _ = _build(
        exposure=D("0"), quotes=[_post_quote("fUST_p2")],
        cap=D("10000"), book_provider=_SnapshotProvider(_ask_snapshot()),
    )
    with caplog.at_level(logging.INFO):
        await rec.deploy()
    assert not any("ladder_would_post" in r.getMessage() for r in caplog.records)
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_rate == 0.00024999


# ---------------------------------------------------------------------------
# SubmitAttemptRecorder wiring (P0 trading-status)
#
# The reconciler already logs every one of these outcomes. These tests pin that
# the same facts also land somewhere the status endpoint can read, because on
# 2026-07-27 "is it placing orders?" was answerable only by grepping logs.
# ---------------------------------------------------------------------------


def _recorder():
    from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
        SubmitAttemptRecorder,
    )
    return SubmitAttemptRecorder()


async def test_guard_block_is_recorded_as_the_last_attempt():
    rec_att = _recorder()
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], safety_allowed=False,
        attempt_recorder=rec_att,
    )
    await rec.deploy()
    assert ex.submitted == []
    assert rec_att.last is not None
    assert rec_att.last.outcome == "blocked"
    assert rec_att.last.guard_name == "fake"
    assert rec_att.last.reason == "blocked"
    assert rec_att.last.cell == "fUST_a30"
    assert rec_att.last.symbol == "fUST"
    assert rec_att.last.amount == D("200")


async def test_successful_submit_is_recorded():
    rec_att = _recorder()
    rec, _ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], attempt_recorder=rec_att,
    )
    await rec.deploy()
    assert rec_att.last is not None
    assert rec_att.last.outcome == "submitted"
    assert rec_att.last.amount == D("200")


async def test_venue_rejection_is_recorded_as_rejected_not_blocked():
    rec_att = _recorder()
    rec, _ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
        executor=_RejectingExecutor(), attempt_recorder=rec_att,
    )
    await rec.deploy()
    assert rec_att.last is not None
    assert rec_att.last.outcome == "rejected"


async def test_submit_exception_is_recorded_as_error():
    class _Boom(_FakeExecutor):
        async def submit(self, decision, ctx, *, cid=None):
            raise RuntimeError("venue 500")

    rec_att = _recorder()
    rec, _ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], executor=_Boom(),
        attempt_recorder=rec_att,
    )
    await rec.deploy()
    assert rec_att.last is not None
    assert rec_att.last.outcome == "error"
    assert "venue 500" in (rec_att.last.reason or "")


async def test_recorder_is_optional_and_absent_changes_nothing():
    # Default construction (attempt_recorder=None) must stay byte-identical:
    # every pre-existing test above builds without one.
    rec, ex, tracker, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")],
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    assert tracker.deployed("fUST_a30") == D("200")
