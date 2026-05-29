# Deployment Reconciler (Standing-Quote) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把放貸的 quote 層（1h signal）與 deployment 層（90s reconcile）解耦——signal 只寫 standing quote、reconciler 成唯一送單者並朝 `target_exposure` 收斂，杜絕靜默 `10001` 並把閒置資金重投延遲從 ~1h 降到 ~90s。

**Architecture:** 新增 `execution/deployment/` 模組（StandingQuoteStore、純函式 sizing/allocation、CellDeploymentTracker intent ledger、DeploymentReconciler）。`SignalEngine.process_candle` 移除 `executor.submit` + safety eval，改寫 StandingQuoteStore。`PeriodicReconcile._tick` 在 reconcile 完成後呼叫 `DeploymentReconciler.deploy()`。safety_chain gate 從 signal 移到 deployment。target_exposure 重用既有 `BFX_ALLOCATION_CAP_USDT`（operator 設 570）。

**Tech Stack:** Python 3.13, asyncio, Decimal, pydantic, pytest (asyncio auto-mode), SQLAlchemy async（既有，本計畫不碰 DB schema）。

**測試指令（一律在 `backend_py/`）:** `cd backend_py && uv run pytest -m "not integration"`；型別 `uv run mypy src/ && uv run ruff check`。

**Deviations from spec（規劃中確認的 v1 簡化，spec 已同步）:**
- Minimum guard 用靜態 `effective_min = ceil(150×1.02) = 153`，不 fetch USDT/USD 行情（同 balance fetch 列 future venue-call）。
- Rate sanity band v1 deferred（與 TTL 冗餘，需 best-ask fetch）。
- signal 層**完全移除** safety eval（不只 submit）——standing quote = 純策略意圖；safety 單一 gate 在 deployment。DECISION diagnostics 因此只記 pre-safety；guard-block 的 forensic parity 列 follow-up。

---

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `src/bfx_funding_bot/modules/execution/deployment/__init__.py` | package marker | Create |
| `src/bfx_funding_bot/modules/execution/deployment/standing_quote.py` | `StandingQuote` + `StandingQuoteStore`（per-cell，TTL，POST-only active） | Create |
| `src/bfx_funding_bot/modules/execution/deployment/sizing.py` | 純函式 `effective_min_usdt` + `allocate_gap` | Create |
| `src/bfx_funding_bot/modules/execution/deployment/tracker.py` | `CellDeploymentTracker`（per-cell intent + 比例校正） | Create |
| `src/bfx_funding_bot/modules/execution/deployment/reconciler.py` | `DeploymentReconciler.deploy()` 串接全部 | Create |
| `src/bfx_funding_bot/modules/marketfeed/signal_engine.py` | 移除 submit+safety，改寫 quote store | Modify |
| `src/bfx_funding_bot/modules/execution/periodic_reconcile.py` | `_tick` 末段呼叫 deployment | Modify |
| `src/bfx_funding_bot/modules/marketfeed/daemon.py` | 接線：建 store/tracker/reconciler、注入 SignalEngine、傳入 PeriodicReconcile、env 旋鈕 | Modify |
| `backend_py/configs/cells.canary.yaml` | 移除 `reference_amount_usdt`、更新註解 | Modify |
| `tests/modules/execution/deployment/test_*.py` | 對應單元測試 | Create |

依賴方向：`standing_quote` / `sizing` / `tracker`（無內部依賴，純）→ `reconciler`（依前三 + 既有 protocols/schemas）→ `daemon` 接線。

---

### Task 1: StandingQuote + StandingQuoteStore

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/deployment/__init__.py`
- Create: `src/bfx_funding_bot/modules/execution/deployment/standing_quote.py`
- Test: `tests/modules/execution/deployment/test_standing_quote.py`

- [ ] **Step 1: 建 package marker**

Create `src/bfx_funding_bot/modules/execution/deployment/__init__.py`（空檔）。
Create `tests/modules/execution/deployment/__init__.py`（空檔）。

- [ ] **Step 2: Write the failing test**

Create `tests/modules/execution/deployment/test_standing_quote.py`:

```python
from uuid import uuid4

from bfx_funding_bot.modules.execution.deployment.standing_quote import (
    StandingQuote,
    StandingQuoteStore,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome


def _post(cell_id: str, created_at_ms: int) -> StandingQuote:
    return StandingQuote(
        cell_id=cell_id,
        outcome=DecisionOutcome.POST,
        rate=0.00012,
        period_days=2,
        signal_correlation_id=uuid4(),
        created_at_ms=created_at_ms,
    )


def test_get_active_returns_fresh_post():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post("fUST_a30", created_at_ms=1_000))
    got = store.get_active("fUST_a30", now_ms=1_000)
    assert got is not None
    assert got.rate == 0.00012
    assert got.period_days == 2


def test_skip_quote_is_not_active():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(
        StandingQuote(
            cell_id="fUST_a30", outcome=DecisionOutcome.SKIP,
            rate=None, period_days=None,
            signal_correlation_id=uuid4(), created_at_ms=1_000,
        )
    )
    assert store.get_active("fUST_a30", now_ms=1_000) is None


def test_expired_quote_is_not_active():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post("fUST_a30", created_at_ms=1_000))
    # now is past created_at + ttl
    assert store.get_active("fUST_a30", now_ms=1_000 + 3_900_001) is None


def test_missing_cell_returns_none():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    assert store.get_active("nope", now_ms=1_000) is None


def test_update_overwrites_previous():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post("fUST_a30", created_at_ms=1_000))
    store.update(_post("fUST_a30", created_at_ms=2_000))
    got = store.get_active("fUST_a30", now_ms=2_000)
    assert got is not None and got.created_at_ms == 2_000
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_standing_quote.py -v`
Expected: FAIL — `ModuleNotFoundError: ...standing_quote`.

- [ ] **Step 4: Write minimal implementation**

Create `src/bfx_funding_bot/modules/execution/deployment/standing_quote.py`:

```python
"""StandingQuote — per-cell standing intent produced by the signal layer.

Decoupling (spec 2026-05-29): the signal layer (1h candle boundary) decides
the *terms* (POST{rate,period} / SKIP) and writes a StandingQuote here. The
deployment reconciler (90s) reads active (POST + non-expired) quotes and
deploys idle capital toward them without recomputing the signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome


@dataclass(frozen=True, slots=True)
class StandingQuote:
    cell_id: str
    outcome: DecisionOutcome          # POST / SKIP
    rate: float | None                # set iff POST
    period_days: int | None           # set iff POST
    signal_correlation_id: UUID
    created_at_ms: int                # wall-clock ms when written


class StandingQuoteStore:
    """In-memory per-cell quote map. Written by signal layer, read by reconciler.

    Not persisted: on cold start it is empty and repopulates at the first candle
    boundary (matches pre-refactor behavior; DECISION forensics live in PG).
    """

    def __init__(self, *, ttl_ms: int = 3_900_000) -> None:
        self._ttl_ms = ttl_ms
        self._quotes: dict[str, StandingQuote] = {}

    def update(self, quote: StandingQuote) -> None:
        self._quotes[quote.cell_id] = quote

    def get_active(self, cell_id: str, *, now_ms: int) -> StandingQuote | None:
        """Return the quote iff it is POST and within TTL; else None."""
        q = self._quotes.get(cell_id)
        if q is None:
            return None
        if q.outcome != DecisionOutcome.POST:
            return None
        if now_ms - q.created_at_ms > self._ttl_ms:
            return None
        return q
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_standing_quote.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment/__init__.py \
        backend_py/src/bfx_funding_bot/modules/execution/deployment/standing_quote.py \
        backend_py/tests/modules/execution/deployment/__init__.py \
        backend_py/tests/modules/execution/deployment/test_standing_quote.py
git commit -m "✨ Feat: StandingQuote + StandingQuoteStore (per-cell, TTL, POST-only active)"
```

---

### Task 2: sizing — effective_min_usdt + allocate_gap (純函式)

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/deployment/sizing.py`
- Test: `tests/modules/execution/deployment/test_sizing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/modules/execution/deployment/test_sizing.py`:

```python
from decimal import Decimal

from bfx_funding_bot.modules.execution.deployment.sizing import (
    allocate_gap,
    effective_min_usdt,
)

D = Decimal


def test_effective_min_static_153():
    assert effective_min_usdt(D("150"), D("0.02")) == D("153")


def test_effective_min_rounds_up():
    # 150 * 1.015 = 152.25 -> ceil -> 153
    assert effective_min_usdt(D("150"), D("0.015")) == D("153")


def _alloc(target, exposure, deployed, active, conc=D("0.70"), min_fill=D("153")):
    return allocate_gap(
        target=target, current_exposure=exposure, deployed=deployed,
        active_cells=active, concentration_pct=conc, min_fill=min_fill,
    )


def test_no_gap_returns_empty():
    assert _alloc(D("570"), D("570"), {}, ["a"]) == {}


def test_gap_below_min_returns_empty():
    # gap = 100 < 153
    assert _alloc(D("570"), D("470"), {}, ["a", "b"]) == {}


def test_single_active_cell_fills_gap_up_to_concentration_cap():
    # gap = 200, cap_per_cell = 0.70*570 = 399 -> fill whole 200 in one offer
    out = _alloc(D("570"), D("370"), {}, ["a"])
    assert out == {"a": D("200")}


def test_concentration_cap_limits_a_single_cell():
    # target 570, cap_per_cell 399. gap = 570 (exposure 0), only cell "a" active.
    # "a" can take at most 399; remaining 171 has no other active cell -> dropped.
    out = _alloc(D("570"), D("0"), {}, ["a"])
    assert out == {"a": D("399")}


def test_fills_emptiest_cell_first_then_next():
    # gap = 570, both active, both empty. emptiest-first (tiebreak cell_id):
    # "a" -> 399 (cap), remaining 171 >= 153 -> "b" -> 171.
    out = _alloc(D("570"), D("0"), {}, ["a", "b"])
    assert out == {"a": D("399"), "b": D("171")}


def test_already_deployed_reduces_headroom():
    # "a" already has 350 deployed -> headroom 399-350 = 49 < 153 -> skip "a";
    # gap = 200, "b" empty -> "b" gets 200.
    out = _alloc(D("570"), D("370"), {"a": D("350")}, ["a", "b"])
    assert out == {"b": D("200")}


def test_no_active_cells_returns_empty():
    assert _alloc(D("570"), D("0"), {}, []) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_sizing.py -v`
Expected: FAIL — `ModuleNotFoundError: ...sizing`.

- [ ] **Step 3: Write minimal implementation**

Create `src/bfx_funding_bot/modules/execution/deployment/sizing.py`:

```python
"""Pure sizing functions for the deployment reconciler.

No I/O, no venue calls — deterministic given inputs (testable in isolation).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal


def effective_min_usdt(venue_floor_usd: Decimal, buffer_pct: Decimal) -> Decimal:
    """Smallest offer (in USDT) that clears the venue's USD-equiv minimum.

    v1: static — the buffer absorbs USDT de-peg + precision (assumes
    USDT >= 1 - buffer). No USDT/USD ticker fetch (deferred, future venue-call).
    ceil(150 * 1.02) = 153.
    """
    raw = venue_floor_usd * (Decimal("1") + buffer_pct)
    return raw.quantize(Decimal("1"), rounding=ROUND_CEILING)


def allocate_gap(
    *,
    target: Decimal,
    current_exposure: Decimal,
    deployed: dict[str, Decimal],
    active_cells: list[str],
    concentration_pct: Decimal,
    min_fill: Decimal,
) -> dict[str, Decimal]:
    """Distribute the funding gap across active cells.

    Greedy emptiest-first (balances per-cell deployment over time), each cell
    capped at concentration_pct * target. Fills below min_fill are dropped to
    avoid sub-minimum dust (would be rejected by the venue minimum anyway).
    Total allocated <= gap, so the global allocation cap is never exceeded.
    """
    gap = target - current_exposure
    if gap < min_fill or not active_cells:
        return {}

    cap_per_cell = concentration_pct * target
    ordered = sorted(active_cells, key=lambda c: (deployed.get(c, Decimal("0")), c))

    fills: dict[str, Decimal] = {}
    remaining = gap
    for cell in ordered:
        if remaining < min_fill:
            break
        headroom = cap_per_cell - deployed.get(cell, Decimal("0"))
        fill = min(remaining, headroom)
        if fill >= min_fill:
            fills[cell] = fill
            remaining -= fill
    return fills
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_sizing.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment/sizing.py \
        backend_py/tests/modules/execution/deployment/test_sizing.py
git commit -m "✨ Feat: deployment sizing — effective_min_usdt + greedy allocate_gap"
```

---

### Task 3: CellDeploymentTracker (intent ledger)

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/deployment/tracker.py`
- Test: `tests/modules/execution/deployment/test_tracker.py`

- [ ] **Step 1: Write the failing test**

Create `tests/modules/execution/deployment/test_tracker.py`:

```python
from decimal import Decimal

from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker

D = Decimal


def test_record_deploy_accumulates():
    t = CellDeploymentTracker()
    t.record_deploy("a", D("200"))
    t.record_deploy("a", D("50"))
    assert t.deployed("a") == D("250")
    assert t.deployed("b") == D("0")


def test_snapshot_is_a_copy():
    t = CellDeploymentTracker()
    t.record_deploy("a", D("100"))
    snap = t.snapshot()
    snap["a"] = D("999")
    assert t.deployed("a") == D("100")


def test_reconcile_to_total_scales_proportionally():
    t = CellDeploymentTracker()
    t.record_deploy("a", D("300"))
    t.record_deploy("b", D("100"))
    # venue truth dropped to 200 (S=400) -> factor 0.5
    t.reconcile_to_total(D("200"))
    assert t.deployed("a") == D("150")
    assert t.deployed("b") == D("50")


def test_reconcile_to_total_noop_when_no_intent():
    t = CellDeploymentTracker()
    # S = 0 but venue has exposure -> cannot attribute, leave per-cell at 0
    t.reconcile_to_total(D("450"))
    assert t.snapshot() == {}


def test_reconcile_to_total_scales_up():
    t = CellDeploymentTracker()
    t.record_deploy("a", D("100"))
    t.reconcile_to_total(D("150"))
    assert t.deployed("a") == D("150")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError: ...tracker`.

- [ ] **Step 3: Write minimal implementation**

Create `src/bfx_funding_bot/modules/execution/deployment/tracker.py`:

```python
"""CellDeploymentTracker — in-memory per-cell deployment intent.

Avoids the venue credit->cell attribution problem (the known fcn-mapping pain):
per-cell amounts come from (a) our own successful submits, and (b) proportional
rescaling to the global venue truth after each reconcile. The global allocation
cap is still enforced against authoritative venue exposure; this tracker only
informs the per-cell concentration limit (best-effort).
"""
from __future__ import annotations

from decimal import Decimal


class CellDeploymentTracker:
    def __init__(self) -> None:
        self._deployed: dict[str, Decimal] = {}

    def deployed(self, cell_id: str) -> Decimal:
        return self._deployed.get(cell_id, Decimal("0"))

    def snapshot(self) -> dict[str, Decimal]:
        return dict(self._deployed)

    def record_deploy(self, cell_id: str, amount: Decimal) -> None:
        self._deployed[cell_id] = self.deployed(cell_id) + amount

    def reconcile_to_total(self, e_total: Decimal) -> None:
        """Rescale per-cell intent so it sums to the venue truth e_total.

        S=0 (no recorded intent yet, e.g. post-restart with pre-existing credits)
        -> no-op; per-cell stays 0 and the concentration limit is best-effort
        until intent rebuilds.
        """
        s = sum(self._deployed.values(), Decimal("0"))
        if s <= 0:
            return
        factor = e_total / s
        self._deployed = {c: v * factor for c, v in self._deployed.items()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_tracker.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment/tracker.py \
        backend_py/tests/modules/execution/deployment/test_tracker.py
git commit -m "✨ Feat: CellDeploymentTracker — per-cell intent w/ proportional reconcile"
```

---

### Task 4: DeploymentReconciler (串接)

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/deployment/reconciler.py`
- Test: `tests/modules/execution/deployment/test_reconciler.py`

依賴的既有型別簽章（已驗證）:
- `DecisionPayload(decision_outcome, signal_correlation_id, offer_rate, offer_amount_usdt, offer_duration_days, budget_seconds=None)` — POST 需 rate/amount/period 非 None（pydantic validator）。
- `ExecutorPort.submit(decision, ctx, *, cid=None) -> SubmittedOrder`。
- safety chain: `evaluate(decision, ctx) -> GuardResult(allowed, guard_name, reason)`。
- `ledger.current_exposure() -> Decimal`。
- `CellConfig.cell_id` (property), `AccountContext.allocation_cap_usdt`。

- [ ] **Step 1: Write the failing test**

Create `tests/modules/execution/deployment/test_reconciler.py`:

```python
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
        timeframe="1h", params={}, reference_amount_usdt=150.0,
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reconciler.py -v`
Expected: FAIL — `ModuleNotFoundError: ...reconciler`.

- [ ] **Step 3: Write minimal implementation**

Create `src/bfx_funding_bot/modules/execution/deployment/reconciler.py`:

```python
"""DeploymentReconciler — the single writer for venue submits.

Called by PeriodicReconcile after each successful reconcile (ledger holds fresh
venue truth). Reads active standing quotes + global exposure, allocates the gap
toward target, applies the full safety chain, and submits. The signal layer no
longer submits (single-writer; spec 2026-05-29).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.execution.deployment.sizing import (
    allocate_gap,
    effective_min_usdt,
)
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    GuardResult,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload

log = logging.getLogger(__name__)


class _LedgerProtocol(Protocol):
    def current_exposure(self) -> Decimal: ...


class _SafetyChainProtocol(Protocol):
    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult: ...


class DeploymentReconciler:
    def __init__(
        self,
        *,
        store: StandingQuoteStore,
        tracker: CellDeploymentTracker,
        ledger: _LedgerProtocol,
        safety_chain: _SafetyChainProtocol,
        executor: ExecutorPort,
        account_ctx: AccountContext,
        cells: list[CellConfig],
        venue_floor_usd: Decimal,
        min_offer_buffer_pct: Decimal,
        concentration_pct: Decimal,
        clock: Callable[[], int],
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._ledger = ledger
        self._safety = safety_chain
        self._executor = executor
        self._ctx = account_ctx
        self._cells = cells
        self._min_fill = effective_min_usdt(venue_floor_usd, min_offer_buffer_pct)
        self._concentration_pct = concentration_pct
        self._clock = clock

    async def deploy(self) -> None:
        now = self._clock()
        e_total = self._ledger.current_exposure()
        # Keep per-cell intent consistent with the authoritative venue truth.
        self._tracker.reconcile_to_total(e_total)

        active = [c.cell_id for c in self._cells
                  if self._store.get_active(c.cell_id, now_ms=now) is not None]

        fills = allocate_gap(
            target=self._ctx.allocation_cap_usdt,
            current_exposure=e_total,
            deployed=self._tracker.snapshot(),
            active_cells=active,
            concentration_pct=self._concentration_pct,
            min_fill=self._min_fill,
        )
        if not fills:
            return

        for cell_id, amount in fills.items():
            quote = self._store.get_active(cell_id, now_ms=now)
            if quote is None:  # defensive: TTL could lapse between checks
                continue
            decision = DecisionPayload(
                decision_outcome=DecisionOutcome.POST,
                signal_correlation_id=quote.signal_correlation_id,
                offer_rate=quote.rate,
                offer_amount_usdt=float(amount),
                offer_duration_days=quote.period_days,
            )
            guard = await self._safety.evaluate(decision, self._ctx)
            if not guard.allowed:
                log.info(
                    "deployment_skip cell=%s amount=%s guard=%s reason=%s",
                    cell_id, amount, guard.guard_name, guard.reason,
                )
                continue
            try:
                await self._executor.submit(decision, self._ctx)
            except Exception:
                log.exception("deployment_submit_failed cell=%s amount=%s", cell_id, amount)
                continue
            self._tracker.record_deploy(cell_id, amount)
            log.info("deployment_submitted cell=%s amount=%s", cell_id, amount)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reconciler.py -v`
Expected: PASS (5 passed).

> Note: if `CellConfig(...)` 建構在測試裡因必填欄位失敗，跑 `cd backend_py && uv run python -c "from bfx_funding_bot.modules.marketfeed.config import CellConfig; help(CellConfig)"` 確認欄位，補齊 `_cell()` 的 kwargs（strategy/symbol/period_agg/timeframe/params/staleness_budget_hours 為已知必要欄位）。

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py \
        backend_py/tests/modules/execution/deployment/test_reconciler.py
git commit -m "✨ Feat: DeploymentReconciler — single-writer gap deploy w/ safety gate"
```

---

### Task 5: SignalEngine — 改寫 quote，移除 submit + safety

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/signal_engine.py:96-188`
- Test: `tests/modules/marketfeed/test_signal_engine.py`（既有，需更新）

- [ ] **Step 1: 找出既有測試裡會破的斷言**

Run: `cd backend_py && grep -n "executor\|submit\|safety\|_apply_safety" tests/modules/marketfeed/test_signal_engine.py tests/modules/marketfeed/test_signal_engine_phase42.py`
這些斷言對應「signal 直接 submit / 跑 safety」的舊行為，本任務移除該行為，故需改寫。記下行號。

- [ ] **Step 2: Write the failing test（新行為：寫 quote、不 submit）**

在 `tests/modules/marketfeed/test_signal_engine.py` 末尾新增（import 若缺則補）：

```python
import pytest

from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome


@pytest.mark.asyncio
async def test_process_candle_writes_post_standing_quote(monkeypatch):
    """POST decision -> StandingQuoteStore gets an active POST quote; no submit."""
    from tests.modules.marketfeed import _signal_engine_fixtures as fx  # 見 Step 3 備註

    store = StandingQuoteStore(ttl_ms=3_900_000)
    engine = fx.build_engine_posting(quote_store=store, clock=lambda: 5_000)
    await engine.process_candle(**fx.post_candle_kwargs())

    q = store.get_active(fx.CELL_ID, now_ms=5_000)
    assert q is not None
    assert q.outcome == DecisionOutcome.POST
    assert q.rate is not None and q.period_days is not None
    assert q.created_at_ms == 5_000
```

> **備註（Step 2 → Step 3 銜接）:** 既有 `test_signal_engine.py` 已有建構 `SignalEngine` + candle 的 helper / fixture。**不要新建 `_signal_engine_fixtures`**——改用該檔既有的建構方式（沿用其 strategy registry、candle、cell fixture），把上面測試改寫成用既有 helper 注入 `quote_store=` 與 `clock=`、斷言 store 內容。若既有測試以「直接 new SignalEngine(...)」建構，照抄該段、加 `quote_store`/`clock` 參數即可。

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_signal_engine.py -k standing_quote -v`
Expected: FAIL — `SignalEngine.__init__() got an unexpected keyword argument 'quote_store'`.

- [ ] **Step 4: 改 SignalEngine.__init__（移除 safety/executor/account_ctx，加 quote_store/clock）**

`src/bfx_funding_bot/modules/marketfeed/signal_engine.py` — 改 `__init__`（行 97-116）。先加 import：

```python
import time
from collections.abc import Callable

from bfx_funding_bot.modules.execution.deployment.standing_quote import (
    StandingQuote,
    StandingQuoteStore,
)
```

把 `__init__` 改為：

```python
    def __init__(
        self,
        *,
        phase: Phase,
        event_sink: _EventSink,
        diagnostics: _DiagnosticsProtocol,
        candles_repo: _CandlesRepoProtocol,
        reporter: DivergenceReporter | None = None,
        quote_store: StandingQuoteStore | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.phase = phase
        self._events = event_sink
        self.diagnostics = diagnostics
        self.candles_repo = candles_repo
        self.reporter = reporter or DivergenceReporter()
        self.quote_store = quote_store
        self._clock = clock or (lambda: int(time.time() * 1000))
```

- [ ] **Step 5: 改 process_candle 尾段（行 168-188）— 不跑 safety、不 submit、改寫 quote**

把行 168-188 整段替換為：

```python
        # Decoupling (spec 2026-05-29): the signal layer records a standing quote
        # (pure strategy intent). Safety gating + venue submit happen in the
        # deployment reconciler (single writer). No executor.submit here.
        decision = self._build_tentative_decision(
            correlation_id, cell, live_signal,
            is_stale=is_stale, stale_seconds=stale_seconds,
        )

        await self._emit_decision_final(correlation_id, cell, decision)

        if self.quote_store is not None:
            self.quote_store.update(StandingQuote(
                cell_id=cell.cell_id,
                outcome=decision.decision_outcome,
                rate=decision.offer_rate,
                period_days=decision.offer_duration_days,
                signal_correlation_id=correlation_id,
                created_at_ms=self._clock(),
            ))
```

刪除現在已無引用的 `_apply_safety_eval` method（行 281-303 附近，整個 method）與其唯一呼叫點（已在上面替換掉）。同時移除 `__init__` 已刪掉的 `safety_chain`/`executor`/`account_ctx` 在檔內其餘引用（grep 確認）：

Run: `cd backend_py && grep -n "self.safety_chain\|self.executor\|self.account_ctx\|_apply_safety_eval\|_SafetyChainProtocol\|ExecutorPort\|AccountContext\|GuardResult" src/bfx_funding_bot/modules/marketfeed/signal_engine.py`
移除/清掉這些引用與對應 import（`ExecutorPort`、`GuardResult`、`_SafetyChainProtocol` 若僅此處用）。`AccountContext` import 若無其他用途一併移除。

- [ ] **Step 6: 更新既有破掉的測試**

把 Step 1 找到的「斷言 submit / safety」測試改寫或刪除：
- 斷言 `executor.submit` 被呼叫的 → 改為斷言 `quote_store.get_active(...)` 為 POST。
- 斷言 safety 降級 POST→SKIP 的 → 移到 Task 4 的 reconciler 測試已覆蓋；此處刪除或改為純策略 SKIP 案例（strategy 自身回 SKIP）。
- 建構 `SignalEngine(...)` 傳 `safety_chain=`/`executor=`/`account_ctx=` 的 → 移除這些 kwargs。

- [ ] **Step 7: Run tests**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_signal_engine.py tests/modules/marketfeed/test_signal_engine_phase42.py -v`
Expected: PASS（含新 standing_quote 測試；舊 submit/safety 測試已改寫/移除）。

- [ ] **Step 8: 型別檢查**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/marketfeed/signal_engine.py`
Expected: no errors（若報未用 import，清掉）。

- [ ] **Step 9: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/signal_engine.py \
        backend_py/tests/modules/marketfeed/test_signal_engine.py \
        backend_py/tests/modules/marketfeed/test_signal_engine_phase42.py
git commit -m "♻️ Refactor: SignalEngine writes standing quote; submit+safety move to deployment"
```

---

### Task 6: PeriodicReconcile — _tick 末段呼叫 deployment

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/periodic_reconcile.py:42-66,112-163`
- Test: `tests/modules/execution/test_periodic_reconcile.py`（既有，新增案例）

設計：在 `_tick` 成功 reconcile（非 failure 早退）後、回傳前，呼叫可選的 `deployment.deploy()`。包在 try/except 內——deployment 失敗絕不可讓對帳骨幹崩潰。前置條件：`recovery.run()` 透過 bus 同步派送 `PositionReconciled` → `ledger.on_position_reconciled` 已更新 exposure（既有 wiring，daemon.py:873）。

- [ ] **Step 1: Write the failing test**

在 `tests/modules/execution/test_periodic_reconcile.py` 新增（沿用檔內既有 `_FakeProbe`、`_FakeRecovery`、`ReconcileResult` import）：

```python
class _FakeDeployment:
    def __init__(self) -> None:
        self.calls = 0

    async def deploy(self) -> None:
        self.calls += 1


async def test_deployment_called_after_clean_reconcile():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 0, 0)])
    deployment = _FakeDeployment()
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.02,
        max_consecutive_failures=3, deployment=deployment,
    )
    import asyncio
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.03)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert deployment.calls >= 1


async def test_deployment_not_called_on_reconcile_failure():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[RuntimeError("venue down")])
    deployment = _FakeDeployment()
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.02,
        max_consecutive_failures=3, deployment=deployment,
    )
    import asyncio
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.03)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert deployment.calls == 0


async def test_deployment_exception_does_not_crash_loop():
    class _BoomDeployment:
        def __init__(self) -> None:
            self.calls = 0

        async def deploy(self) -> None:
            self.calls += 1
            raise RuntimeError("deploy boom")

    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 0, 0)])
    deployment = _BoomDeployment()
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.02,
        max_consecutive_failures=3, deployment=deployment,
    )
    import asyncio
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert deployment.calls >= 1  # loop kept ticking despite the exception
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py -k deployment -v`
Expected: FAIL — `PeriodicReconcile.__init__() got an unexpected keyword argument 'deployment'`.

- [ ] **Step 3: Implement**

在 `periodic_reconcile.py` 加 deployment protocol（檔頂 protocol 區）：

```python
class _Deployment(Protocol):
    async def deploy(self) -> None: ...
```

`__init__`（行 45-66）新增參數與儲存：

```python
        deployment: _Deployment | None = None,
```
在 body 末尾加：
```python
        self._deployment = deployment
```

`_tick`（行 112-163）末尾——在所有 health-status 處理之後、method 結束前——加：

```python
        if self._deployment is not None:
            try:
                await self._deployment.deploy()
            except Exception:  # deployment must never crash the reconcile backbone
                log.exception("deployment_phase_failed")
```

（注意：此段在 `result = await self._recovery.run()` 成功路徑內；failure 路徑在行 127 已 `return`，不會走到這。）

- [ ] **Step 4: Run tests**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py -v`
Expected: PASS（既有 + 3 新案例全過）。

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py \
        backend_py/tests/modules/execution/test_periodic_reconcile.py
git commit -m "✨ Feat: PeriodicReconcile invokes DeploymentReconciler after clean reconcile"
```

---

### Task 7: Daemon 接線 + config

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`（多處，見下）
- Modify: `backend_py/configs/cells.canary.yaml`

- [ ] **Step 1: 注入 quote_store + clock 到 SignalEngine，移除 safety/executor**

`daemon.py` SignalEngine 建構（行 929-937）改為：

```python
    quote_store = StandingQuoteStore(
        ttl_ms=int(os.environ.get("BFX_QUOTE_TTL_MS", "3900000")),
    )
    signal_engine_obj = SignalEngine(
        phase=config.phase,
        event_sink=stdout_sink,
        diagnostics=diagnostics,
        candles_repo=_CandlesRepoBridge(),
        quote_store=quote_store,
        clock=lambda: int(time.time() * 1000),
    )
```

檔頂加 import：
```python
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker
from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
```

- [ ] **Step 2: 建 DeploymentReconciler，接進 PeriodicReconcile**

DeploymentReconciler 需 `wrapped_executor`（行 875 建好）、`safety_chain`（行 771）、`ledger`、`account_ctx`、`config.cells`、`quote_store`。它必須在 `wrapped_executor` 之後建構。把 `periodic_reconcile = PeriodicReconcile(...)`（行 838-843，在 `if not spec.is_simulated:` 內）往後搬到 `wrapped_executor` 建好之後（行 883 之後），並改為：

```python
        deployment_reconciler = DeploymentReconciler(
            store=quote_store,
            tracker=CellDeploymentTracker(),
            ledger=ledger,
            safety_chain=safety_chain,
            executor=wrapped_executor,
            account_ctx=account_ctx,
            cells=config.cells,
            venue_floor_usd=Decimal(os.environ.get("BFX_VENUE_FLOOR_USD", "150")),
            min_offer_buffer_pct=Decimal(os.environ.get("BFX_MIN_OFFER_BUFFER_PCT", "0.02")),
            concentration_pct=Decimal(os.environ.get("BFX_CONCENTRATION_PCT", "0.70")),
            clock=lambda: int(time.time() * 1000),
        )
        periodic_reconcile = PeriodicReconcile(
            recovery=runtime_recovery,
            probe=probe,
            interval_s=reconcile_interval_s,
            min_resync_interval_s=resync_min_interval_s,
            deployment=deployment_reconciler,
        )
```

> 順序約束：`quote_store` 在 Step 1（行 929 區）建立，但 `signal_engine_obj` 在 daemon 中目前晚於 `wrapped_executor`（行 875）與 reconcile 區（行 838）。**把 `quote_store = StandingQuoteStore(...)` 這行往前提到 `wrapped_executor` 之前**（例如緊接 `bus = DomainEventBus()` 行 783 之後），讓 reconcile 區與 SignalEngine 都能引用同一個 instance。確認 `quote_store` 只建一次、兩處共用。

- [ ] **Step 3: 移除 cells.canary.yaml 的 reference_amount_usdt**

`backend_py/configs/cells.canary.yaml` — 刪掉兩個 cell 的 `reference_amount_usdt: 150.0` 行，並把頂部 Sizing note 段（`# Sizing note ...` 那段）替換為：

```yaml
# Sizing (deployment reconciler, spec 2026-05-29-deployment-reconciler):
# Single-order size is no longer set here. The deployment reconciler fills the
# gap toward target_exposure (= BFX_ALLOCATION_CAP_USDT, set 570 on Koyeb) using
# greedy emptiest-first allocation, per-cell capped at 70% of target, each offer
# >= effective_min (ceil(150*1.02)=153 USDT) to clear the venue USD minimum.
```

（`reference_amount_usdt` 在 `CellConfig` 仍有 default=150.0，移除 yaml 行不會壞 parsing；該欄位現為 unused，留待後續清理。）

- [ ] **Step 4: 跑全套單元測試 + 型別**

Run: `cd backend_py && uv run pytest -m "not integration"`
Expected: PASS（全綠）。若 daemon 有整合/啟動測試引用舊 SignalEngine 簽章，依 Task 5 同樣方式修正。

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: no errors。

- [ ] **Step 5: 驗證 config 可載入**

Run: `cd backend_py && BFX_CELLS_YAML=configs/cells.canary.yaml uv run python -c "from bfx_funding_bot.modules.marketfeed.config import load_config; print('cells:', [c.pair_id for c in load_config().cells])"`
Expected: 印出 2 個 fUST pair_id，無 validation error。

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py \
        backend_py/configs/cells.canary.yaml
git commit -m "✨ Feat: wire DeploymentReconciler + StandingQuoteStore into daemon; drop reference_amount_usdt"
```

---

## 部署（程式碼合併後，operator 手動）

> Koyeb auto-deploy 已停用（`no_deploy_on_push=true`），且改 Koyeb env 會觸發真錢 redeploy。以下為 operator 步驟，不在程式碼任務內。

1. Koyeb env 設 `BFX_ALLOCATION_CAP_USDT=570`（從 450 上調；留 ~5% buffer vs ~$600 錢包）。
2. 跑 `scripts/deploy-koyeb.sh canary`（注意 deploy script 可能每次重設 cap——確認 script 不會把 570 覆寫回 450，必要時改 script 的 cap 行）。
3. 驗證：Koyeb log 出現 `deployment_submitted cell=... amount=...`；下個 90s tick 後閒置資金重投；`position_state` realized 上升、無 `10001`。

---

## Self-Review

**1. Spec coverage:**
- Quote 層只寫 quote、不 submit → Task 5 ✓
- Deployment 層 = 唯一送單者、朝 target 收斂 → Task 4 + Task 6 ✓
- Minimum guard（靜態 153，杜絕 10001）→ Task 2 `effective_min_usdt` ✓
- Greedy + 70% 集中度上限 → Task 2 `allocate_gap` ✓
- StandingQuote TTL / SKIP 不部署 → Task 1 ✓
- safety_chain gate 移到 deployment → Task 4（呼叫 evaluate）+ Task 5（signal 移除）✓
- Per-cell intent ledger + 比例校正（避開 venue→cell 歸屬）→ Task 3 ✓
- single-writer（reconciler 唯一送單）→ Task 5（移除 signal submit）+ Task 6 ✓
- target = cap 570（重用 BFX_ALLOCATION_CAP_USDT）→ Task 4（讀 account_ctx.allocation_cap_usdt）+ 部署段 ✓
- 移除 reference_amount_usdt → Task 7 Step 3 ✓
- Out-of-scope（balance fetch / rate sanity / per-currency）→ 不實作，已記 spec + ROADMAP ✓

**2. Placeholder scan:** 無 TBD / 「add error handling」；每個 code step 有完整 code。Task 5 Step 2 的 fixture 銜接已用備註明確指向「沿用既有 helper」而非佔位。

**3. Type consistency:** `StandingQuoteStore.get_active(cell_id, *, now_ms)`、`allocate_gap(*, target, current_exposure, deployed, active_cells, concentration_pct, min_fill)`、`CellDeploymentTracker.{deployed,snapshot,record_deploy,reconcile_to_total}`、`DeploymentReconciler.deploy()`、`PeriodicReconcile(..., deployment=)` 在跨 task 引用一致；`DecisionPayload` POST 欄位（offer_rate/offer_amount_usdt/offer_duration_days）與 schema validator 一致；`ExecutorPort.submit(decision, ctx, *, cid=None)` 簽章一致。
