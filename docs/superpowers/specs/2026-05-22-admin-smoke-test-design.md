# Phase 4.4 prework — Admin Smoke-Test Endpoint

**Status**: design
**Date**: 2026-05-22
**Author**: Will (brainstorm w/ Claude Opus 4.7)
**Predecessors**: 2026-05-22-phase4.3-executor-middleware-design.md (Phase 4.3 ship; ADR compressed in `~/second-brain/wiki/projects/bfx-funding-bot/decisions/2026-05-22-phase4.3-executor-middleware.md`)
**Successors (planned)**: Phase 4.4 BitfinexLive (smoke live mode 屆時 extend)
**Origin**: Phase 4.3 ADR Followup R 段 — `POST /admin/smoke-test` deploy-time 觸合成 paper offer 走完整 chain, assert ledger / Axiom react. Phase 4.4 prework 必做。

---

## 1. Goals & Non-goals

### Goals

1. 對 Phase 4.3 ship 的 executor middleware chain（HeartbeatMW > ReservationEmittingMW > TransientRetryMW > EchoPaperExecutor）建立 deploy-time 端到端驗證能力，補 4.3 ADR Followup R 段「Axiom 端 RESERVATION_CLAIMED / ORDER_FILL event types 出現」的驗證需求
2. 提供 boot-time auto smoke（deploy gate）+ on-demand admin endpoint（ad-hoc verify）兩種觸發路徑，共用同一 SmokeRunner core
3. Smoke synthetic event 完整隔離 prod state — 0 污染 prod ledger / 0 影響 AllocationCap / 仍真 emit 到 Axiom 以驗證 ingest pipeline
4. 為 Phase 4.4 live executor 接入時的 smoke live mode 留 API 擴展點（hardcoded paper params + `?mode=` query param hook）
5. 順帶補 Phase 5+ multi-tenant 必要的 ledger handler account_id guard（pre-prod 改成本 ≈ 0）

### Non-goals（本 spec 不做）

- 4.4 live executor smoke mode（會新增 `?mode=paper|live` + SmokeRunner branch；屆時另開 spec）
- 24/7 continuous synthetic monitoring（cron 持續打 smoke）— 此 spec 只做 deploy-time + on-demand
- Smoke 結果 dashboard / alerting — Axiom 端用 `account_id="smoke_test"` query 已足夠
- Multi-account / per-tenant smoke — Phase 5+ multi-tenant 才有意義
- DLQ / outbox for smoke event publish — 同 4.3 ADR Followup，Phase 5+
- Smoke 結果 historical aggregation / SLO 統計 — out of scope

---

## 2. Background & Industry Spectrum

### 2.1 為什麼需要 smoke-test endpoint

Phase 4.3 ship `dfece968` Koyeb deploy HEALTHY，prod log 4 個 code path proof 表明 deploy 成功，但這只證 process 起來 + Axiom ingest 200 — **沒有任何 deploy-time gate 證明 executor middleware chain 端到端正確 wire**。

具體會漏抓的 failure modes:
- 改 daemon.py wiring 順序（middleware 包裝順序錯）→ unit test 過、deploy 後 ledger / sink 不收事件
- 改 ReservationEmittingMW + bus.subscribe 但漏改另一邊 → publish 沒人收
- Axiom credentials drift（rotate 後忘改 env）→ emit 真的 silently 失敗（client 內部 buffer）
- Phase 4.4 接 live executor 時，wiring 漏 ReservationEmittingMW 直接 inner → bus 不 fire

Smoke 是 deploy 後的 invariant verification — synthesize paper offer 走 chain，assert observable side effects（recorder events + Axiom round-trip）。

### 2.2 業界 spectrum — Deploy verification 模式

| 模式 | 例子 | 適合? |
|---|---|---|
| Health endpoint only (`/healthz`) | k8s liveness probe、Koyeb health check | 不夠 — 只證 process 活、不證 business logic |
| Boot-time self-check | Spring Boot ApplicationRunner、etcd boot sanity、k8s init container | 是 deploy gate 的最強形式 |
| Admin / control-plane endpoint | Spinnaker post-deploy verify、自家 `POST /admin/*` | 適合 ad-hoc verify / env var 改後 re-verify |
| Synthetic monitoring (24/7 cron) | Datadog Synthetic、Checkly、Stripe 內部 | 抓 drift 但持續產生雜訊 + 成本 |
| **Boot smoke + admin endpoint 複合**（採納） | Stripe production readiness、Netflix canary + smoke | **modern best practice**：deploy gate（fail-fast）+ escape hatch |

採納理由：boot smoke 是 deploy gate fail-fast；endpoint 是 4.4 / 後續環境變更時不需 redeploy 即可重驗；複合 cost 在 pre-prod 約 = 單做一個。

### 2.3 業界 spectrum — Synthetic event isolation

| 模式 | 例子 | Trade-off |
|---|---|---|
| **獨立 account_id / tenant**（採納） | Stripe `acct_test_*`、Datadog Synthetic 專用 workspace、Salesforce sandbox | 最小 schema 變動；下游 filter 自然；smoke history Axiom 可 audit |
| Payload flag `is_test=true` | PayPal sandbox、Twilio | 每 consumer 必須 check flag，遺漏風險 |
| 獨立 dataset / topic | Kafka topic-per-tenant、多 warehouse | 物理隔離但 2x cost、lifecycle 分開 |
| In-memory simulation (mock axiom_sink) | pytest 風 CI smoke | 零污染但不驗 Axiom ingest pipeline — 違背 4.3 ADR Followup R 段需求 |
| 複合 (tag + prefix + TTL) | AWS X-Ray test span | overkill for solo pre-prod |

採納理由：`ledger.replay_from_axiom` 既有 `account_id` filter（`ledger.py:135`），用 `account_id="smoke_test"` 零改動隔離 replay 路徑；live update 路徑補 handler-level guard（順帶 Phase 5+ multi-tenant 必要）。

### 2.4 業界 spectrum — Bus subscribe / unsubscribe API

| 模式 | 例子 | Trade-off |
|---|---|---|
| Subscribe-only | 簡單 pub/sub lib | 強迫 subscription lifetime = 系統 lifetime；smoke ephemeral 用例會洩漏 |
| Subscribe 回傳 disposal token | RxJS `Subscription`、Redux `store.subscribe` | reactive world 主流 |
| 顯式 `unsubscribe(type, handler)` | Node.js EventEmitter、Vert.x、Spring | 對稱但 try/finally boilerplate |
| **`subscription()` async context manager**（採納） | OpenTelemetry SDK scope、Trio nurseries | modern async Python 最佳；exception-safe；結構化併發風格 |
| Weak references | Java WeakListenerList | auto-GC unsubscribe；subtle bugs |

採納理由：smoke 是「scoped subscription」典型場景，`async with bus.subscription(EventType, handler):` 跑完自動 cleanup、exception path 不漏；既有 `subscribe()` 不破壞用於 permanent wirings (ledger / axiom_sink)。

### 2.5 業界 spectrum — Smoke verification 載體

| 模式 | 例子 | Trade-off |
|---|---|---|
| Ephemeral domain object（小 ledger） | — | 對稱但 ledger 含 replay 機制 smoke 用不到、概念混亂 |
| **Spy / Recorder pattern**（採納） | Greg Young ES fixtures、Axon `Fixture.expectEvents()`、Stripe internal smoke | 只記事件序列、focused、最小手腳 |
| Snapshot / Restore prod state | — | async race-prone、brittle |
| Mock-all-the-way | unit test 風格 | 跟 unit 重複、不驗 wiring |

採納理由：smoke 驗的是「chain 把對的 event 對的 payload 對的順序丟到 bus」，不是「exposure 算對」（後者由 ledger unit test 涵蓋）。`EventRecorder`（30 行 dataclass）零概念負債。

---

## 3. Architecture

### 3.1 新模組

```
src/bfx_funding_bot/modules/admin/
  __init__.py
  smoke_runner.py     # SmokeRunner class + EventRecorder + module-level lock — core logic (boot + endpoint 共用)
  router.py           # FastAPI APIRouter — POST /admin/smoke-test
  axiom_query.py      # AxiomEventQueryAdapter — 實作 _AxiomQueryProtocol，發 APL query 撈 order/reservation events
tests/modules/admin/
  test_event_recorder.py
  test_smoke_runner.py
  test_router.py
  test_axiom_query.py
tests/integration/
  test_smoke_full_chain.py
  test_admin_smoke_http.py
```

`smoke_runner.py` 與 `router.py` 分離：boot smoke 不經 HTTP、必須能直接 import 呼叫 `run_l2()`；HTTP layer 只是薄殼。

### 3.2 修改現有檔案

| 檔案 | 改動 |
|---|---|
| `modules/execution/bus.py` | 新增 `subscription(event_type, handler)` async context manager（subscribe on enter / unsubscribe on exit / exception 也 unsubscribe） |
| `modules/execution/ledger.py` | 3 handler (`on_reservation_claimed` / `on_order_filled` / `on_reservation_released`) 加 `if event.account_id != self.account_id: return` 守衛 |
| `modules/marketfeed/healthz.py:make_app()` | 簽名擴充：`smoke_runner: SmokeRunner \| None = None`、`admin_token: str \| None = None`；二者皆 truthy 時 mount admin router |
| `modules/marketfeed/healthz.py:run_healthz_server()` | 簽名擴充：往下傳 smoke_runner / admin_token |
| `modules/marketfeed/daemon.py:build_daemon()` | 構造 SmokeRunner（注入 wrapped_executor、bus、axiom_client、新建的 `AxiomEventQueryAdapter`、envelope phase/strategy/cell）；加上 Daemon dataclass field |
| `modules/admin/axiom_query.py`（新檔） | 新增 `AxiomEventQueryAdapter` — 實作 `_AxiomQueryProtocol`，發 APL query 到 Axiom REST 撈 ORDER_SUBMIT / ORDER_FILL / RESERVATION_* events by account_id + since timestamp。借 `smoke/g1.py:AxiomQueryClient` 的 httpx 模式，但限縮到 order/reservation events 用 |
| `modules/marketfeed/daemon.py:Daemon._healthz_server_loop()` | 傳遞 smoke_runner + admin_token 給 `run_healthz_server()` |
| `modules/marketfeed/daemon.py:_run()` | `build_daemon()` 後 / `daemon.run()` 前插入 boot smoke try/except 區塊 |

### 3.3 Smoke chain 策略：prod chain 重用 + account_id 隔離

- Smoke 呼叫 prod 既有 `wrapped_executor.submit()`，DecisionPayload 與 AccountContext 帶 `account_id="smoke_test"`
- Prod ledger（account_id=`"default"`）的 3 個 handler 因新 account_id guard skip smoke event
- SmokeRunner 用 `EventRecorder(account_id="smoke_test")` ephemeral subscribe 到同一 bus；recorder filter 只收 smoke account_id 的 event
- Axiom sink 不分 account_id，正常 emit（這是要的 — L3 round-trip 驗證依賴）

### 3.4 兩種觸發路徑

**Boot smoke** — `_run()` 內，`build_daemon()` 之後、`daemon.run()` 之前：

```python
async def _run() -> None:
    daemon = await build_daemon()
    # NEW: boot smoke L2 here (pre-TaskGroup)
    try:
        await asyncio.wait_for(daemon.smoke_runner.run_l2(), timeout=30.0)
        log.info("smoke_boot_passed")
    except (TimeoutError, Exception) as exc:
        log.critical("smoke_boot_failed err=%r — daemon continues", exc)
        await daemon.axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.CRITICAL.value,
            "phase": daemon.config.phase.value,
            "strategy": None, "cell": None,
            "event_type": EventType.SAFETY_TRIGGER.value,
            "correlation_id": str(uuid4()),
            "payload": {"check_target": "smoke_boot", "reason": repr(exc)},
        })
    # daemon 無條件繼續 — smoke 自己 bug 不能鎖死 daemon
    stop = daemon._stop_event
    ...
    await daemon.run()
```

Pre-TaskGroup 是刻意：executor / bus / axiom 已 wired（build_daemon 已完成），但還沒進入 supervision context — smoke 失敗不會觸發 TaskGroup cancel。

**Admin endpoint** — daemon healthz uvicorn (port 8080) 內 mount `POST /admin/smoke-test`：
- `Authorization: Bearer $BFX_ADMIN_TOKEN` 驗證
- Module-level `asyncio.Lock` single-flight；持鎖時第二個 request 回 409
- 預設跑 L3（in-process + Axiom round-trip）；`?level=L2` query param 可跳 round-trip

### 3.5 SmokeRunner API skeleton

```python
class SmokeRunner:
    def __init__(
        self,
        *,
        executor: ExecutorPort,           # wrapped chain (prod chain 重用)
        bus: DomainEventBus,
        axiom_client: _AxiomProtocol,     # for boot-time SAFETY_TRIGGER emit
        axiom_query: _AxiomQueryProtocol, # for L3 round-trip query
        phase: Phase, strategy: StrategyName, cell: str,
    ) -> None: ...

    async def run_l2(self) -> SmokeResult: ...   # in-process only
    async def run_l3(self) -> SmokeResult: ...   # L2 + Axiom round-trip

@dataclass(frozen=True)
class SmokeResult:
    status: Literal["pass", "fail"]
    level: Literal["L2", "L3"]
    checks: dict[str, Any]   # 每個 check 的 bool + 細節
    duration_ms: int
    error: str | None = None  # 若 exception
```

### 3.6 EventRecorder

```python
@dataclass
class EventRecorder:
    account_id: str
    events: list[Any] = field(default_factory=list)

    async def record(self, event: Any) -> None:
        if getattr(event, "account_id", None) == self.account_id:
            self.events.append(event)
```

### 3.7 bus.subscription() context manager

```python
@asynccontextmanager
async def subscription(
    self, event_type: type, handler: EventHandler,
) -> AsyncIterator[None]:
    self.subscribe(event_type, handler)
    try:
        yield
    finally:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
```

---

## 4. Data Flow

### 4.1 L3 endpoint sequence

```
POST /admin/smoke-test  (Authorization: Bearer $BFX_ADMIN_TOKEN)
  ├─ router auth check
  │    ├─ missing header → 401 {"error": "missing_auth"}
  │    └─ wrong token    → 403 {"error": "unauthorized"}
  ├─ asyncio.Lock try_acquire (non-blocking)
  │    └─ already held   → 409 {"error": "smoke_already_running"}
  └─ SmokeRunner.run_l3()
       ├─ recorder = EventRecorder(account_id="smoke_test")
       ├─ async with bus.subscription(ReservationClaimed,  recorder.record), \
       │            bus.subscription(OrderFilled,         recorder.record), \
       │            bus.subscription(ReservationReleased, recorder.record):
       │     ├─ decision  = DecisionPayload(
       │     │     decision_outcome=DecisionOutcome.POST,
       │     │     signal_correlation_id=uuid4(),
       │     │     offer_rate=0.0001,
       │     │     offer_amount_usdt=1.0,
       │     │     offer_duration_days=2)
       │     ├─ smoke_ctx = AccountContext(
       │     │     account_id="smoke_test",
       │     │     credentials=Credentials("smoke", "smoke"),
       │     │     allocation_cap_usdt=Decimal("1000"))
       │     ├─ before_ts = datetime.now(UTC)
       │     ├─ result    = await wrapped_executor.submit(decision, smoke_ctx)
       │     │     └─ HeartbeatMW → ReservationEmittingMW → TransientRetryMW → EchoPaperExecutor
       │     │         ↓ bus.publish(ReservationClaimed[account_id=smoke_test])
       │     │             ├─ recorder.record() ✓
       │     │             ├─ prod_ledger handler — skipped via account_id guard
       │     │             └─ axiom_sink → axiom.emit(real Axiom ingest)
       │     │         ↓ bus.publish(OrderFilled[account_id=smoke_test])
       │     │             ├─ recorder.record() ✓
       │     │             ├─ prod_ledger handler — skipped
       │     │             └─ axiom_sink → axiom.emit
       │     └─ L2 assertions on recorder + result
       ├─ (handlers auto-unsubscribed on context exit)
       └─ L3 round-trip poll:
            ├─ max 15s (5 attempts × 3s):
            │     events = await axiom_query.query_order_events("smoke_test", before_ts)
            │     if len(events) >= 2 and event_types ⊇ {RESERVATION_CLAIMED, ORDER_FILL}: break
            └─ timeout → L3 fail (L2 already pass)
  → return JSON SmokeResult
  ← release lock
```

### 4.2 L2 assertions

```python
# After executor.submit returns
assert result.status == "filled"
assert len(recorder.events) == 2
assert isinstance(recorder.events[0], ReservationClaimed)
assert recorder.events[0].size_usdt == Decimal("1.0")
assert recorder.events[0].account_id == "smoke_test"
assert recorder.events[0].is_simulated is True
assert isinstance(recorder.events[1], OrderFilled)
assert recorder.events[1].size_usdt == Decimal("1.0")
assert recorder.events[1].account_id == "smoke_test"
assert recorder.events[1].is_simulated is True
```

### 4.3 L3 assertions

```python
# After L2 passes, poll Axiom up to 15s
events = await axiom_query.query_order_events("smoke_test", since=before_ts)
event_types = {e["event_type"] for e in events}
assert len(events) >= 2
assert EventType.RESERVATION_CLAIMED.value in event_types
assert EventType.ORDER_FILL.value in event_types
# 不 assert == 2，因為 Axiom 可能短時 dedup 或重送
```

---

## 5. Error Handling

### 5.1 HTTP error matrix

| 失敗 | HTTP | Body |
|---|---|---|
| Missing `Authorization` header | 401 | `{"error": "missing_auth"}` |
| Token mismatch | 403 | `{"error": "unauthorized"}` |
| Lock held（concurrent smoke） | 409 | `{"error": "smoke_already_running"}` |
| `BFX_ADMIN_TOKEN` env 未設 | endpoint 不掛載（startup 跳過 + log warn） | — |
| executor.submit raise | 200 | `{"status": "fail", "level": "L2", "checks": {"executor_returned": false, ...}, "error": "..."}` |
| L2 event count / payload wrong | 200 | `{"status": "fail", "level": "L2", "checks": {...}}` |
| L3 round-trip timeout | 200 | `{"status": "fail", "level": "L3", "checks": {l2 pass, l3 timeout}}` |
| 全 pass | 200 | `{"status": "pass", "level": "L3", "checks": {...}, "duration_ms": ...}` |

### 5.2 為什麼 L2/L3 fail 回 200 不回 503

業界 split：Stripe smoke / AWS deploy verify / Datadog Synthetic 都用 200 + status field（endpoint 本身工作，被測物失敗是 body 內容）；503 適合 `curl --fail` 場景。

採 200 因 CI script 一律 `curl + jq '.status'` 比 `--fail` 多狀態好處理；且區分「endpoint broken (5xx)」vs「smoke failed (200 + status:fail)」訊號乾淨。

### 5.3 Boot smoke 失敗處置

```python
try:
    await asyncio.wait_for(daemon.smoke_runner.run_l2(), timeout=30.0)
    log.info("smoke_boot_passed")
except (TimeoutError, Exception) as exc:
    log.critical("smoke_boot_failed err=%r — daemon continues", exc)
    await daemon.axiom.emit({...SAFETY_TRIGGER critical with reason...})
# daemon 無條件繼續
```

**Invariant: smoke crash ≠ daemon crash.** Smoke 自己有 bug 不能鎖死 prod；daemon 任何狀態都進 TaskGroup。失敗訊號透過 Axiom SAFETY_TRIGGER critical event 觸達操作員。

---

## 6. Testing Strategy

### 6.1 Unit tests

| 檔案 | 測試範圍 |
|---|---|
| `tests/modules/execution/test_bus.py`（追加） | `subscription()` ctx manager：enter subscribe / exit unsubscribe / exception body 也 unsubscribe / 與既有 `subscribe()` duplicate-detection 不衝突 |
| `tests/modules/execution/test_ledger.py`（追加） | 3 handler 的 account_id guard：foreign account_id → `_reserved` / `_realized` 不變；matching → 行為不變（regression） |
| `tests/modules/admin/test_event_recorder.py` | `record()` filter（matching account_id collect / foreign skip）；events 順序保留；不 mutate event |
| `tests/modules/admin/test_smoke_runner.py` | `run_l2()` happy + sad（executor raise / event count wrong / size wrong / account_id wrong）；`run_l3()` happy + sad（axiom_query timeout / raise）；module-level lock single-flight |
| `tests/modules/admin/test_router.py` | 401 / 403 / 409 / 200 各路徑；TOKEN 未設 → endpoint 不掛載 |

### 6.2 Integration tests

| 檔案 | 範圍 |
|---|---|
| `tests/integration/test_smoke_full_chain.py` | 真 DomainEventBus + ReservationEmittingMW + AxiomEventSink + prod_ledger (account_id="default") + smoke recorder + **fake AxiomClient**（in-memory list-of-emits recorder，conforming `_AxiomProtocol`）：跑 `run_l2()`，assert (a) prod ledger 完全不動 (b) recorder 收 2 事件 (c) fake axiom 收 2 emit 且 account_id="smoke_test" |
| `tests/integration/test_admin_smoke_http.py` | FastAPI TestClient against `make_app(smoke_runner=..., admin_token=...)`：error matrix 全跑；single-flight 409 用 asyncio.gather 兩 concurrent request 驗證 |

---

## 7. Edge Cases

| # | 情境 | 處理 |
|---|---|---|
| E1 | `BFX_ADMIN_TOKEN` 未設 | Endpoint 不掛載 + log warn；boot smoke 仍跑（無需 token） |
| E2 | Smoke 觸發時 prod signal 同時 fire | 各自 account_id tag；recorder + prod_ledger 各自 filter；bus / sink 共享但無 cross-contamination |
| E3 | Boot smoke 30s timeout | `asyncio.wait_for(..., 30.0)`；超時 → SAFETY_TRIGGER + 繼續；多半代表 Axiom buffer 滿，daemon 繼續讓 Axiom 恢復 |
| E4 | Endpoint 在 boot smoke 還沒跑完時被打 | 不會發生：endpoint 屬於 healthz uvicorn（TaskGroup 內），boot smoke 在 TaskGroup 前跑；嚴格時序 |
| E5 | Smoke 跑時 HeartbeatMW 記錄 executor heartbeat | 預期且想要 — 額外的 health check side benefit |
| E6 | SafetyChain hard guards 是否會 block smoke offer | 不會：smoke 直呼 `executor.submit`，不經 signal_engine；SafetyChain 在 signal_engine 內呼叫。Smoke 是 wiring check 不是 policy check |
| E7 | Phase=shadow 下 executor 沒被 prod 用，smoke 還能跑嗎 | 能：smoke 自己呼叫 executor，與 phase 無關 |
| E8 | `is_simulated` 在 4.4 live executor 接入後變 False | Out of scope — 此 spec 只支援 paper smoke；4.4 live smoke mode 屆時另開 spec / 加 `?mode=` 參數 |
| E9 | 多次 smoke 在 Axiom 累積污染 | 接受 — Axiom 30d retention 自動 GC；用 `account_id="smoke_test"` 可作 deploy audit log |

---

## 8. Invariants

| ID | Invariant | 驗證 |
|---|---|---|
| I1 | Smoke 0 污染 prod state | Integration test：smoke 前後 prod ledger `_reserved` + `_realized` 完全相等 |
| I2 | 每次 smoke 觸發恰好 2 個 `bus.publish`（1 ReservationClaimed + 1 OrderFilled） | recorder count assertion |
| I3 | Endpoint 無 auth 不可達 | router unit + integration test |
| I4 | Boot smoke crash ≠ daemon crash | `_run()` 包 try/except；daemon `await daemon.run()` 在 smoke 任何 exception 後仍執行 |
| I5 | axiom_sink emit 兩個事件且 account_id="smoke_test" | Integration：stub axiom client 收 emit；assert call args |
| I6 | Smoke single-flight | unit + concurrent integration 兩種 |
| I7 | Phase 5+ multi-tenant guard ready | ledger handler account_id check 已落 — 加 tenant 不需 backfill |

---

## 9. Forward-compat / Extension Points

- **4.4 live smoke mode**：`SmokeRunner.run_l2/l3` 接受 `mode: Literal["paper", "live"] = "paper"`；live mode 用 0.000001 USDT 微量真 offer 驗 live executor wiring + WS fcn 接收。Endpoint `?mode=live` query param。
- **24/7 synthetic monitoring**：可直接外部 cron `curl -X POST /admin/smoke-test`；無需 code 改動，配 Axiom alert 在 SAFETY_TRIGGER critical event 觸發 PagerDuty。
- **Multi-tenant smoke**：Phase 5+ smoke runner 注入 tenant_id；endpoint 路徑改 `/admin/{tenant_id}/smoke-test`。當前 single-tenant `account_id="smoke_test"` 即是「tenant=smoke_test」的特例。
- **Outbox / DLQ smoke**：Phase 5+ 改寫 bus.publish 後，smoke 自動受惠（驗證 publish 端到端含 outbox）。

---

## 10. Open Questions / Risk

- **Q1 (resolved)**: 現行 `daemon._AxiomQueryAdapter` 是 4.3 stub 回 `[]`（line 526-546），不能用於 L3 round-trip。決議：**新建 `AxiomEventQueryAdapter`（`modules/admin/axiom_query.py`）**，借 `smoke/g1.py:AxiomQueryClient` 的 httpx + APL pattern，發 query 撈 RESERVATION_CLAIMED / ORDER_FILL events by account_id + since。**不動 ledger 的 stub adapter** — ledger replay 切換到 real query 屬於另一個獨立決策（會把 LedgerReplayError 從理論變操作風險），需另開 ADR；smoke 與 ledger 各自的 query adapter 共用 `_AxiomQueryProtocol`，未來合併零阻力。
- **Q2**: Boot smoke 30s timeout 是否夠？Axiom client buffer flush 一般 < 1s，但 cold start 第一次 emit 可能 SDK init 較慢；30s 給足 margin。若 prod 觀察到 boot smoke 常 timeout，調整為 60s。
- **Q3**: Endpoint mount 在 healthz uvicorn 與 healthz path conflict 風險？healthz 用 `docs_url=None, redoc_url=None, openapi_url=None`，admin router 用 prefix `/admin/`，無 conflict。
- **Q4**: APL query 要 query 多久前的事件？SmokeRunner 自己記 `before_ts = datetime.now(UTC)` 在 submit 前，APL query 用 `_time > {before_ts.isoformat()}` filter — 只看本次 smoke 產生的事件，不會被歷史 smoke 干擾。

---

## Related

- 前置 ADR：[`~/second-brain/wiki/projects/bfx-funding-bot/decisions/2026-05-22-phase4.3-executor-middleware.md`](../../../../../second-brain/wiki/projects/bfx-funding-bot/decisions/2026-05-22-phase4.3-executor-middleware.md) — Phase 4.3 ship + R 段 Followup
- 後續：Phase 4.4 BitfinexLive spec（pending）
