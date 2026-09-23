# E1 Stale-Offer Cancel/Reprice Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓掛在 venue 上不成交的 stale offer 被自動 cancel，釋放的 reserved 資金在下一個 ~90s tick 以現行 standing quote 重掛 — 修掉 2026-07-06 profit review 的頭號 CONFIRMED-high leak（stale offer 永久卡死資金於 0% 收益）。

**Architecture:** 純 policy 函式（`reprice.py`：哪些 offer 該砍）+ `DeploymentReconciler.deploy()` 開頭的 sweep（執行，維持 single-writer）。Venue offers 由 `BootRecovery.run()` 既有的 snapshot 經 `ReconcileResult` 傳入 `deploy(venue_offers=...)`（零額外 REST call）。Cancel 走已建好的 `BitfinexLiveExecutor.cancel`（CancelRequested → REST → CancelAcknowledged；release 由 WS foc / 下次 reconcile 收斂 — 本 plan 不動 ledger）。預設 observe-only（`BFX_REPRICE_ENABLED=false` 只 log 不砍），canary 觀察 ≥24h 後翻 true。

**Tech Stack:** Python 3.13 + uv、pytest（asyncio auto mode，直接寫 `async def test_*`）、mypy、ruff。

**背景（實作者必讀，5 分鐘）：** `backend_py/docs/research/2026-07-06-profit-design-review.md` §0 + §1 E1 段。架構圖：`backend_py/ARCHITECTURE.md` §3b、§9。

## Global Constraints

- 所有指令在 `backend_py/` 下跑：`cd backend_py && uv run ...`（repo root 會撞 pyenv 3.12）。
- **I-SW single-writer**：只有 `DeploymentReconciler` 對 venue 提交/取消；sweep **絕不**直接改 ledger / tracker / position（release 由 WS foc 與 90s reconcile 收斂）。
- **Fail-safe**：`ExecutorAuthError` 必須 propagate（daemon exit 78）；其他 cancel 錯誤 log 後跳過，**sweep 失敗不得擋住後續 gap 部署**。
- **Anti-chase policy（不可放寬）**：只 reprice-DOWN（offer rate 高於現行 active quote 超過 tolerance 才砍）；offer 齡 < min_age 不砍（spike 當小時不自追）；無 active quote 的 symbol 一律不砍（resting 高價單 = 免費 spike option）；每 tick 最多 `max_cancels_per_tick` 筆。
- 每個 commit 前：`uv run pytest -m "not integration"` 全綠 + `uv run mypy src/` + `uv run ruff check` 無誤。
- Commit 訊息用 repo emoji prefix（`✨ Feat:` / `✅ Test:` / `📝 Docs:`）。
- 新 env vars（全帶安全預設，未設定 = 現狀行為）：`BFX_REPRICE_ENABLED`（default false）、`BFX_REPRICE_TOLERANCE_PCT`（default 0.10）、`BFX_REPRICE_MIN_AGE_S`（default 1800）、`BFX_REPRICE_MAX_CANCELS_PER_TICK`（default 3）。

---

### Task 1: Reprice policy 純函式

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/deployment/reprice.py`
- Test: `tests/modules/execution/deployment/test_reprice.py`

**Interfaces:**
- Consumes: `ActiveFundingOffer`（`external/bitfinex/auth_rest.py:26` — 欄位 `venue_offer_id: str, symbol: str, amount: Decimal, rate: float, period_days: int, mts_created: int, status: str`）
- Produces: `RepricePolicy`（frozen dataclass：`enabled: bool, tolerance_pct: float, min_age_ms: int, max_cancels_per_tick: int`）、`stale_offers(*, offers, ref_rate, now_ms, policy) -> list[ActiveFundingOffer]`、`policy_from_env(environ) -> RepricePolicy` — Task 3/4 依賴這些名稱與簽名。

- [ ] **Step 1: Write the failing tests**

```python
# tests/modules/execution/deployment/test_reprice.py
from decimal import Decimal

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.deployment.reprice import (
    RepricePolicy,
    policy_from_env,
    stale_offers,
)

_NOW = 10_000_000
_POLICY = RepricePolicy(
    enabled=True, tolerance_pct=0.10, min_age_ms=1_800_000, max_cancels_per_tick=3,
)


def _offer(
    voi: str = "1", rate: float = 0.001, age_ms: int = 3_600_000, symbol: str = "fUST",
) -> ActiveFundingOffer:
    return ActiveFundingOffer(
        venue_offer_id=voi, symbol=symbol, amount=Decimal("200"), rate=rate,
        period_days=2, mts_created=_NOW - age_ms, status="ACTIVE",
    )


def test_overpriced_old_offer_is_stale():
    # ref 0.0002, offer 0.001 = 5x → 遠超 +10% tolerance，齡 60min ≥ 30min
    got = stale_offers(offers=[_offer()], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY)
    assert [o.venue_offer_id for o in got] == ["1"]


def test_within_tolerance_not_stale():
    # offer 僅高於 ref 5%（tolerance 10%）→ 留著
    got = stale_offers(
        offers=[_offer(rate=0.00021)], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert got == []


def test_exactly_at_threshold_not_stale():
    # 邊界：恰等於 ref*(1+tol) 不砍（嚴格大於才砍）
    got = stale_offers(
        offers=[_offer(rate=0.00022)], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert got == []


def test_below_quote_never_stale():
    # 低於現行 quote 的 offer 即將被市場吃掉，不砍
    got = stale_offers(
        offers=[_offer(rate=0.0001)], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert got == []


def test_young_offer_not_stale():
    # anti-chase：spike 當小時（齡 10min < 30min）即使超價也不砍
    got = stale_offers(
        offers=[_offer(age_ms=600_000)], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert got == []


def test_sorted_most_overpriced_first():
    got = stale_offers(
        offers=[_offer(voi="a", rate=0.0005), _offer(voi="b", rate=0.002)],
        ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert [o.venue_offer_id for o in got] == ["b", "a"]


def test_policy_from_env_defaults():
    p = policy_from_env({})
    assert p == RepricePolicy(
        enabled=False, tolerance_pct=0.10, min_age_ms=1_800_000, max_cancels_per_tick=3,
    )


def test_policy_from_env_enabled_variants():
    for truthy in ("1", "true", "TRUE", "yes"):
        assert policy_from_env({"BFX_REPRICE_ENABLED": truthy}).enabled is True
    assert policy_from_env({"BFX_REPRICE_ENABLED": "false"}).enabled is False


def test_policy_from_env_overrides():
    p = policy_from_env({
        "BFX_REPRICE_TOLERANCE_PCT": "0.05",
        "BFX_REPRICE_MIN_AGE_S": "600",
        "BFX_REPRICE_MAX_CANCELS_PER_TICK": "1",
    })
    assert p.tolerance_pct == 0.05
    assert p.min_age_ms == 600_000
    assert p.max_cancels_per_tick == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reprice.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bfx_funding_bot.modules.execution.deployment.reprice'`

- [ ] **Step 3: Write the implementation**

```python
# src/bfx_funding_bot/modules/execution/deployment/reprice.py
"""Stale-offer reprice policy (E1 — docs/research/2026-07-06-profit-design-review.md §1).

純 policy：哪些 resting venue offer 該 cancel，讓 reserved 資金能以現行
standing quote 重掛。執行（venue call）由 DeploymentReconciler 負責
（single-writer）；本模組零 I/O。

Policy = reprice-DOWN only，anti-chase by construction：
  cancel iff offer.rate > ref_rate * (1 + tolerance_pct)
       and (now_ms - offer.mts_created) >= min_age_ms
ref_rate = 該 symbol 所有 active POST quote 的最高 rate（只要還有任何 cell
願意掛到那個價位，offer 就不算 stale）。低於 quote 的 offer 會自然成交，
不砍；quote 只在 1h boundary 更新，配合 min_age 使 spike 當小時不會自追。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer


@dataclass(frozen=True, slots=True)
class RepricePolicy:
    enabled: bool               # False = observe-only（log would_cancel，不打 venue）
    tolerance_pct: float        # 0.10 = offer rate 高於 quote 10% 以上才砍
    min_age_ms: int             # 齡低於此值一律不砍（anti-chase）
    max_cancels_per_tick: int   # 跨 symbol 的每 tick 上限（churn bound）


def policy_from_env(environ: Mapping[str, str]) -> RepricePolicy:
    """從 env 建 policy；全部未設定 = observe-only 安全預設。"""
    return RepricePolicy(
        enabled=environ.get("BFX_REPRICE_ENABLED", "false").lower() in ("1", "true", "yes"),
        tolerance_pct=float(environ.get("BFX_REPRICE_TOLERANCE_PCT", "0.10")),
        min_age_ms=int(float(environ.get("BFX_REPRICE_MIN_AGE_S", "1800")) * 1000),
        max_cancels_per_tick=int(environ.get("BFX_REPRICE_MAX_CANCELS_PER_TICK", "3")),
    )


def stale_offers(
    *,
    offers: list[ActiveFundingOffer],
    ref_rate: float,
    now_ms: int,
    policy: RepricePolicy,
) -> list[ActiveFundingOffer]:
    """回傳 stale-high 且夠老的 offers，最超價的排最前（cancel budget 先救
    卡最多收益的資金）。嚴格大於 threshold 才算 stale。"""
    threshold = ref_rate * (1.0 + policy.tolerance_pct)
    out = [
        o for o in offers
        if o.rate > threshold and (now_ms - o.mts_created) >= policy.min_age_ms
    ]
    out.sort(key=lambda o: o.rate, reverse=True)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reprice.py -v`
Expected: 10 PASS

- [ ] **Step 5: Quality gates + commit**

```bash
cd backend_py && uv run mypy src/ && uv run ruff check
git add src/bfx_funding_bot/modules/execution/deployment/reprice.py tests/modules/execution/deployment/test_reprice.py
git commit -m "✨ Feat: E1 reprice policy 純函式（reprice-down only + min-age anti-chase）"
```

---

### Task 2: 把 venue offers 從 reconcile 傳進 deploy + CancelPort protocol

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/protocols.py`（加 `CancelPort`）
- Modify: `src/bfx_funding_bot/modules/execution/boot_recovery.py:51-61`（`ReconcileResult` 加欄位）+ `:383-390`（return 帶 offers）
- Modify: `src/bfx_funding_bot/modules/execution/periodic_reconcile.py:42-44`（`_Deployment` protocol）+ `:170-174`（`_tick` 傳參）
- Modify: `tests/modules/execution/test_periodic_reconcile.py:224-228, 272-276`（fake 簽名跟上）
- Test: `tests/modules/execution/test_periodic_reconcile.py`（新增 pass-through 測試）

**Interfaces:**
- Consumes: `ActiveFundingOffer`、既有 `ReconcileResult`（frozen slots dataclass）
- Produces: `CancelPort`（runtime_checkable Protocol，簽名 = `BitfinexLiveExecutor.cancel`，`live_executor.py:257-264`）：`async def cancel(self, *, venue_offer_id: str, signal_correlation_id: UUID, account_id: str, ctx: AccountContext) -> None`；`ReconcileResult.venue_offers: tuple[ActiveFundingOffer, ...] = ()`；`deploy(*, venue_offers: tuple[ActiveFundingOffer, ...] = ())` 呼叫慣例 — Task 3 依賴。

- [ ] **Step 1: Write the failing test**

在 `tests/modules/execution/test_periodic_reconcile.py` 檔尾加（import 沿用該檔既有的 `ReconcileResult` import；`_FakeRecovery`/`_FakeProbe` 等 fake 沿用該檔既有定義 — 先讀該檔開頭確認名稱後照用）：

```python
async def test_deploy_receives_venue_offers_from_reconcile():
    from decimal import Decimal

    from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer

    offer = ActiveFundingOffer(
        venue_offer_id="42", symbol="fUST", amount=Decimal("200"), rate=0.001,
        period_days=2, mts_created=0, status="ACTIVE",
    )

    class _OfferRecovery:
        async def run(self) -> ReconcileResult:
            return ReconcileResult(
                n_claimed=0, n_released=0, n_failed=0, venue_offers=(offer,),
            )

    class _CapturingDeployment:
        def __init__(self) -> None:
            self.received: list[tuple] = []

        async def deploy(self, *, venue_offers=()) -> None:
            self.received.append(venue_offers)

    dep = _CapturingDeployment()
    pr = PeriodicReconcile(
        recovery=_OfferRecovery(), probe=_FakeProbe(), interval_s=90,
        deployment=dep,
    )
    await pr._tick()
    assert dep.received == [(offer,)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py::test_deploy_receives_venue_offers_from_reconcile -v`
Expected: FAIL — `TypeError: ReconcileResult.__init__() got an unexpected keyword argument 'venue_offers'`

- [ ] **Step 3: Implement**

(a) `protocols.py` — 檔頭 import 補 `from uuid import UUID` 與 `runtime_checkable`（該檔已有 `from typing import Any, Protocol`，改成 `from typing import Any, Protocol, runtime_checkable`），在 `ExecutorPort` 定義後加：

```python
@runtime_checkable
class CancelPort(Protocol):
    """Venue funding-offer cancel（只有 live executor 實作；paper 無 venue offer）。

    對應 BitfinexLiveExecutor.cancel：CancelRequested/CancelAcknowledged audit
    與 release 路徑（WS foc → ReservationReleased）都在那一側，呼叫方不碰 ledger。
    """
    async def cancel(
        self,
        *,
        venue_offer_id: str,
        signal_correlation_id: UUID,
        account_id: str,
        ctx: AccountContext,
    ) -> None: ...
```

(b) `boot_recovery.py` — `ReconcileResult` 加最後一個欄位（frozen+slots dataclass，tuple 預設安全）：

```python
@dataclass(frozen=True, slots=True)
class ReconcileResult:
    n_claimed: int
    n_released: int
    n_failed: int
    reserved_usdt: Decimal = Decimal("0")
    realized_usdt: Decimal = Decimal("0")
    available_usdt: Decimal = Decimal("0")
    n_credits: int = 0
    reserved_drift_usdt: Decimal = Decimal("0")
    realized_drift_usdt: Decimal = Decimal("0")
    venue_offers: tuple[ActiveFundingOffer, ...] = ()
```

`run()` 尾端 return（`:383`）補 `venue_offers=tuple(all_offers),`。

(c) `periodic_reconcile.py` — import 補 `from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer`；`_Deployment` 改：

```python
class _Deployment(Protocol):
    async def deploy(
        self, *, venue_offers: tuple[ActiveFundingOffer, ...] = (),
    ) -> None: ...
```

`_tick()` 尾端改：

```python
        if self._deployment is not None:
            try:
                await self._deployment.deploy(venue_offers=result.venue_offers)
            except Exception:  # deployment must never crash the reconcile backbone
                log.exception("deployment_phase_failed")
```

(d) 既有 fakes 跟上簽名 — `tests/modules/execution/test_periodic_reconcile.py:228` 與 `:276` 的 `async def deploy(self) -> None:` 都改成 `async def deploy(self, *, venue_offers=()) -> None:`（函式體不變）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py -v`
Expected: 全 PASS（含新測試）

- [ ] **Step 5: Quality gates + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
git add -u && git add src/bfx_funding_bot/modules/execution/protocols.py
git commit -m "✨ Feat: E1 CancelPort protocol + ReconcileResult 帶 venue offers 進 deploy()"
```

---

### Task 3: DeploymentReconciler sweep（執行層）

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/deployment/reconciler.py`（constructor + `deploy()` 開頭 sweep + `_reprice_sweep`）
- Test: `tests/modules/execution/deployment/test_reconciler.py`（新增 sweep 測試 + `_build` 擴充）

**Interfaces:**
- Consumes: Task 1 的 `RepricePolicy`/`stale_offers`、Task 2 的 `CancelPort` 與 `deploy(*, venue_offers=())`、`ExecutorAuthError`（`core/errors.py`）
- Produces: `DeploymentReconciler(..., canceller: CancelPort | None = None, reprice: RepricePolicy | None = None)`；`deploy(*, venue_offers: tuple[ActiveFundingOffer, ...] = ())` — Task 4 daemon wiring 依賴。`reprice=None` 或 `canceller=None` 時行為與現狀 byte-identical（除 observe log）。

- [ ] **Step 1: Write the failing tests**

在 `tests/modules/execution/deployment/test_reconciler.py` 加（檔頭 import 補 `from bfx_funding_bot.core.errors import ExecutorAuthError`、`from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer`、`from bfx_funding_bot.modules.execution.deployment.reprice import RepricePolicy`）：

```python
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
    rec, ex, _, _ = _build(
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
```

`_build` 擴充（`test_reconciler.py:142` 起的既有 helper，加兩個參數並傳給 constructor；其餘不動）：

```python
def _build(*, exposure, quotes, safety_allowed=True, executor=None, safety=None,
           available=None, event_sink=None, canceller=None, reprice=None):
    ...
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reconciler.py -v -k sweep`
Expected: FAIL — `TypeError: DeploymentReconciler.__init__() got an unexpected keyword argument 'canceller'`

- [ ] **Step 3: Implement**

`reconciler.py` 修改（依既有 style）：

(a) imports 補：

```python
from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.deployment.reprice import (
    RepricePolicy,
    stale_offers,
)
```

並把 `CancelPort` 加進 protocols import 區塊（`from bfx_funding_bot.modules.execution.protocols import (AccountContext, CancelPort, ExecutorPort, GuardResult, SubmittedOrder)`）。

(b) constructor 參數表尾端（`phase: Phase,` 之後）加：

```python
        canceller: CancelPort | None = None,
        reprice: RepricePolicy | None = None,
```

`__init__` 體加：

```python
        self._canceller = canceller
        self._reprice = reprice
```

(c) `deploy()` 簽名改為：

```python
    async def deploy(
        self, *, venue_offers: tuple[ActiveFundingOffer, ...] = (),
    ) -> None:
        now = self._clock()
        cancel_budget = (
            self._reprice.max_cancels_per_tick if self._reprice is not None else 0
        )
```

(d) symbol 迴圈內，`active = [...]` 計算之後、`fills = allocate_gap(...)` 之前插入：

```python
            # E1 reprice sweep：先於 allocation。cancel 的 release 由 WS foc /
            # 下次 reconcile 收斂（single-writer ledger），本 tick 的 gap 不變，
            # 釋放資金在下一個 ~90s tick 重掛 — 永不 same-tick double-commit。
            if self._reprice is not None and venue_offers:
                cancel_budget -= await self._reprice_sweep(
                    symbol=symbol,
                    symbol_cells=symbol_cells,
                    venue_offers=venue_offers,
                    now=now,
                    budget=cancel_budget,
                )
```

(e) class 尾端加方法：

```python
    async def _reprice_sweep(
        self,
        *,
        symbol: str,
        symbol_cells: list[CellConfig],
        venue_offers: tuple[ActiveFundingOffer, ...],
        now: int,
        budget: int,
    ) -> int:
        """砍掉 rate 已 stale-high 的 resting offers（policy 見 reprice.py）。

        回傳實際發出的 cancel 數（observe mode 恆 0）。任何非 auth 錯誤只
        log 不擋部署（sweep 是 best-effort 最佳化，deploy 才是主線）。
        """
        assert self._reprice is not None
        quotes = [
            q for q in (
                self._store.get_active(c.cell_id, now_ms=now) for c in symbol_cells
            )
            if q is not None and q.rate is not None
        ]
        if not quotes:
            # 無 active POST quote：resting 高價單 = 免費 spike option，留著。
            # 下一個 POST quote 出現時本 sweep 自然會 reprice-down。
            return 0
        ref = max(quotes, key=lambda q: q.rate or 0.0)
        assert ref.rate is not None  # POST quote 的 rate 必非 None
        candidates = stale_offers(
            offers=[o for o in venue_offers if o.symbol == symbol],
            ref_rate=ref.rate,
            now_ms=now,
            policy=self._reprice,
        )
        issued = 0
        for offer in candidates:
            age_min = (now - offer.mts_created) / 60_000
            if not self._reprice.enabled or self._canceller is None:
                log.info(
                    "reprice_would_cancel voi=%s symbol=%s offer_rate=%s "
                    "ref_rate=%s age_min=%.0f",
                    offer.venue_offer_id, symbol, offer.rate, ref.rate, age_min,
                )
                continue
            if issued >= budget:
                log.info(
                    "reprice_budget_exhausted symbol=%s deferred=%d",
                    symbol, len(candidates) - issued,
                )
                break
            try:
                await self._canceller.cancel(
                    venue_offer_id=offer.venue_offer_id,
                    signal_correlation_id=ref.signal_correlation_id,
                    account_id=self._ctx.account_id,
                    ctx=self._ctx,
                )
            except ExecutorAuthError:
                raise  # auth 壞掉必須讓 daemon fail-safe 退出，不可吞
            except Exception:
                log.exception("reprice_cancel_error voi=%s", offer.venue_offer_id)
                continue
            issued += 1
            log.info(
                "reprice_cancelled voi=%s symbol=%s offer_rate=%s ref_rate=%s "
                "age_min=%.0f",
                offer.venue_offer_id, symbol, offer.rate, ref.rate, age_min,
            )
        return issued
```

注意：cancel 的 `signal_correlation_id` 用觸發 reprice 的現行 quote 的 scid（語意 =「因這個新訊號而取消」），CancelRequested audit 事件可回溯到肇因訊號。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/ -v`
Expected: 全 PASS（既有 + 新增；既有測試不帶 venue_offers → sweep 不觸發 → 行為不變）

- [ ] **Step 5: Quality gates + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
git add -u
git commit -m "✨ Feat: E1 DeploymentReconciler stale-offer reprice sweep（observe-mode 預設）"
```

---

### Task 4: Daemon wiring + env + 文件

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py:1034-1057`（DeploymentReconciler 建構處，在 `if not spec.is_simulated:` 區塊內）
- Modify: `../docker-compose.bot.yml`（repo root — 若該檔逐一列 env 才需要，見 Step 2）
- Modify: `ARCHITECTURE.md` §4（放貸演算法）與 §9（invariants）

**Interfaces:**
- Consumes: Task 1 `policy_from_env`、Task 2 `CancelPort`、Task 3 constructor 參數
- Produces: 部署後 daemon 帶 sweep（預設 observe-only）

- [ ] **Step 1: Wire daemon**

`daemon.py` 的 `if not spec.is_simulated:` 區塊內、`deployment_reconciler = DeploymentReconciler(` 之前加 import 與 policy（import 放檔頭既有 import 區）：

```python
from bfx_funding_bot.modules.execution.deployment.reprice import policy_from_env
from bfx_funding_bot.modules.execution.protocols import CancelPort
```

`DeploymentReconciler(...)` 建構加兩參數（`phase=config.phase,` 之後）：

```python
            # E1 reprice sweep：canceller 用 RAW executor（middleware onion 只包
            # submit；cancel 的 audit/retry 已在 BitfinexLiveExecutor 內建）。
            # isinstance(CancelPort) 是 runtime_checkable 結構檢查 — paper
            # executor 無 cancel → None → sweep 恆 noop（defense-in-depth，
            # 本區塊本來就 live-only）。
            canceller=executor if isinstance(executor, CancelPort) else None,
            reprice=policy_from_env(os.environ),
```

- [ ] **Step 2: Compose env passthrough（條件式）**

讀 repo root `docker-compose.bot.yml` 的 `bfx-bot` service：若 env 是逐一列舉（找 `BFX_KILL_SWITCH` 或 `BFX_RECONCILE_INTERVAL_S` 的寫法），照同樣格式加四行 `BFX_REPRICE_ENABLED` / `BFX_REPRICE_TOLERANCE_PCT` / `BFX_REPRICE_MIN_AGE_S` / `BFX_REPRICE_MAX_CANCELS_PER_TICK`（值先全用預設，`BFX_REPRICE_ENABLED=false` 顯式寫出方便 rollout 翻旗）；若是 `env_file` 整檔注入則 compose 不用改，改對應 env file。

- [ ] **Step 3: Run full gates**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: 全綠（daemon wiring 由既有 boot/daemon 測試覆蓋建構路徑；若有 daemon 建構測試因新參數失敗，跟上簽名）

- [ ] **Step 4: Update ARCHITECTURE.md**

§4 部署段 step 7 之後加一步：

```markdown
7b. **Reprice sweep（E1）**：allocation 前，對每個 symbol 比對 venue snapshot 的 resting offers 與現行 active quote：offer rate 高於最高 active quote rate ×(1+`BFX_REPRICE_TOLERANCE_PCT`) 且齡 ≥ `BFX_REPRICE_MIN_AGE_S` → `executor.cancel`（每 tick ≤ `BFX_REPRICE_MAX_CANCELS_PER_TICK` 筆；`BFX_REPRICE_ENABLED=false` 時僅 log `reprice_would_cancel`）。release 由 WS foc / 下次 reconcile 收斂，釋放資金下一 tick 以新 quote 重掛。無 active quote 的 symbol 不砍（resting 高價單留作 spike option）。
```

§9 加一條 invariant：

```markdown
- **I-RP reprice-down only**：sweep 只砍「高於現行 active quote 超過 tolerance 且夠老」的 offer；不砍低於 quote 的、不砍 spike 當小時的（min-age）、無 active quote 不砍。cancel 失敗 fail-safe（offer 留在 book）；`ExecutorAuthError` propagate。sweep 不直接改 ledger/tracker。
```

同段順手修文件債：§4 參數表 Allocation cap `570` → `10000`（env 註記 commit `aa4842c`）、§8 基礎設施表 Koyeb/Neon/Vercel → VM 自托（對齊 repo root CLAUDE.md 部署架構表）。

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "✨ Feat: E1 daemon wiring（BFX_REPRICE_* env，預設 observe-only）+ 📝 ARCHITECTURE §4/§9 + 部署文件債修正"
```

---

### Task 5: Canary rollout（人工 gate — 不可由 subagent 執行）

- [ ] **Step 1: Observe mode 部署**：merge + push 後照現行 VM 部署流程 redeploy `bfx-bot`（`BFX_REPRICE_ENABLED` 維持 false）。
- [ ] **Step 2: 觀察 ≥24h**：`docker logs bfx-bot 2>&1 | grep reprice_would_cancel` —
  - 驗證：只有真正 stale 的 offer 被列為 candidate（rate 明顯高於現行 quote、齡 ≥30min）；同一 offer 不應在 quote 未變時反覆進出候選（churn 訊號）。
  - 若空輸出且期間確實無 stale offer（rates 平穩）→ 屬正常；可暫調 `BFX_REPRICE_TOLERANCE_PCT=0.01` 觀察 predicate 有反應後調回。
- [ ] **Step 3: Enable**：`BFX_REPRICE_ENABLED=true` → redeploy → 觀察 `reprice_cancelled` log + diagnostics `CANCEL_AUDIT`（CancelRequested/CancelAcknowledged 成對）+ 下一 tick 是否以新 quote 重掛（`deployment_submitted`）。
- [ ] **Step 4: 成功判準（1 週）**：無 cancel 風暴（每日 cancel 數 ≈ rate regime 變動次數，非每 tick 都砍）；`position_state.reserved` 無異常歸零；成交延遲（submit→fill）分佈無惡化。
- [ ] **Rollback**：`BFX_REPRICE_ENABLED=false` redeploy 即回 observe（程式碼可留）。

---

## Self-Review 紀錄（writing-plans 時已跑）

- Spec 覆蓋：review §1 E1 的 predicate（rate-deviation + min-age + SKIP 不砍 + budget）、CancelPort、snapshot 傳遞、dry-run rollout 全部有對應 task。SKIP/expired-quote 主動砍單被**刻意排除**（EV-ambiguous：resting 高價單是免費 spike option；stranding 已被「下一個 POST quote 觸發 reprice-down」bound 住）— 與 review 建議的差異已在 policy docstring 記錄。
- Placeholder 掃描：無 TBD/「適當處理」；所有 code step 附完整 code。
- 型別一致：`CancelPort.cancel` 簽名 = `BitfinexLiveExecutor.cancel`（keyword-only，`live_executor.py:257`）；`venue_offers: tuple[ActiveFundingOffer, ...]` 貫穿 Task 2/3；`stale_offers` 名稱在 Task 1/3 一致。
