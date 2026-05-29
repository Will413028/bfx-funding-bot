from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
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
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, StrategyName

D = Decimal


class _FakeLedger:
    def __init__(self, exposure: Decimal) -> None:
        self._e = exposure

    def current_exposure(self) -> Decimal:
        return self._e


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


def _build(*, exposure, quotes, safety_allowed=True, executor=None):
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    for q in quotes:
        store.update(q)
    tracker = CellDeploymentTracker()
    ex = executor or _FakeExecutor()
    safety = _FakeSafety(allowed=safety_allowed)
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=_FakeLedger(exposure),
        safety_chain=safety, executor=ex, account_ctx=_ctx(), cells=cells,
        venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), clock=lambda: 1_000,
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

    rec, ex, tracker, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], executor=_Boom(),
    )
    await rec.deploy()  # must not raise
    assert tracker.deployed("fUST_a30") == D("0")
