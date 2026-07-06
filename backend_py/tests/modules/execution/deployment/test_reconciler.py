import logging
from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
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
from bfx_funding_bot.modules.marketfeed.config import CellConfig
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

    async def emit(self, event: dict) -> None:
        self.events.append(event)


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

    async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
        self.submitted.append(decision)
        return SubmittedOrder(cid=1, venue_offer_id="x", status="submitted", raw_response=None)


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
           available=None, event_sink=None, canceller=None, reprice=None):
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    for q in quotes:
        store.update(q)
    tracker = CellDeploymentTracker()
    ex = executor or _FakeExecutor()
    safety = safety if safety is not None else _FakeSafety(allowed=safety_allowed)
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=_FakeLedger(exposure, available=available),
        safety_chain=safety, executor=ex, account_ctx=_ctx(), cells=cells,
        venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), balance_buffer_usdt=D("3"),
        clock=lambda: 1_000,
        event_sink=event_sink if event_sink is not None else _CapturingSink(),
        phase=Phase.CANARY,
        canceller=canceller,
        reprice=reprice,
    )
    return rec, ex, tracker, safety


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
    # gap = 570; available is effectively unbounded so balance never binds. A single
    # active cell caps at concentration 0.70*570 = 399, leaving 171 stranded — the
    # genuine concentration/no-further-active-cell case must keep its label.
    rec, _ex, _, _ = _build(
        exposure=D("0"), quotes=[_post_quote("fUST_a30")], available=D("1000000"),
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
