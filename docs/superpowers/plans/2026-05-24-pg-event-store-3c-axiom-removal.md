# PG Event Store 3c — Axiom 全移除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除 bfx-funding-bot 對 Axiom（logging/event SaaS）的所有依賴 —— 刪 client/adapter/sink、operational 事件改 structured stdout、L3 smoke 改查 PG `event_log`、退役 G1/paper smoke、`deployment_environment` 從 `AxiomConfig` 解耦、env/CI 清理。

**Architecture:** Strangler Fig 的 **contract step**。3a（A2 write-ahead + recovery）、3b（diagnostics 表）已 ship，所有 forensic/SoT emit 路徑已 repoint 到 PG。3c 是純收尾：先建替代物（`StdoutEventSink`、`PostgresSmokeQueryAdapter`、config 解耦），repoint 所有 consumer 脫離 `AxiomClient`，最後純刪 Axiom。每個 commit 後 `pytest -m "not integration"` 必須全綠（expand → repoint → contract，過程不留斷裂）。

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 async, pytest, ports-and-adapters（`emit(dict)` port 在 3b 已與 `AxiomClient` 對齊，故 3c repoint 只換 wiring identifier）。

---

## 背景：為何此計畫成立（執行前必讀）

3b 後現況（已驗證）：
- **SoT 事件**（`RESERVATION_INTENT/CLAIMED/FAILED`、`ORDER_FILL`、`RESERVATION_RELEASED`）由 `EventStorePersister` 在 command txn 內寫 `event_log`（A2）。**不經 Axiom**。
- **forensic 事件**（`DECISION`/`SAFETY_TRIGGER`/`CANCEL_AUDIT`）由 `DiagnosticsSink` 寫 PG `diagnostics` 表（3b）。**不經 Axiom**。
- **仍掛 Axiom 的只剩 operational telemetry**：`SIGNAL`/`SIGNAL_DIVERGENCE`/`HEALTH_CHECK`/`ORDER_SUBMIT`/`ORDER_FILL`(telemetry) + boot lifecycle，全經 `AxiomClient.emit(dict)`。spec §13.1 item 9：這些 3c 改 **structured stdout**（不建表、非迷你 observability）。
- **`axiom_sink.AxiomEventSink`** 的 `reservation_*` 訂閱仍在但 emit 已是**冗餘**（event_log 才是 SoT）；`handle_cancel_*` 已 unsubscribe（dead）。整個 sink 3c 刪。
- **replay 路徑**（`axiom_event_query.AxiomReplayQueryAdapter` + ledger/registry 的 `replay_from_axiom()`）3a 後 boot 已改走 `from_snapshot`，全 dead。
- **L3 smoke**（`smoke_runner.py` HTTP endpoint `/smoke-test?level=L3`）poll Axiom 找 `RESERVATION_CLAIMED`+`ORDER_FILL` —— 這兩型**有進 event_log** → 改查 PG read-your-writes（spec item 3 + §8）。
- **G1 gate**（`smoke/g1.py`）+ **`scripts/paper_smoke_runner.py`** 查 `signal` 事件 —— signal 3c 後走 stdout 無可查後端 → **整個退役刪除**（使用者 2026-05-24 拍板；它們是手動 pre-deploy CLI，不在 CI）。

**關鍵 invariant**：每個 task 結束 commit 前 `cd backend_py && uv run pytest -m "not integration"` 全綠；deletion task 額外跑 `uv run mypy src/ && uv run ruff check`。**行號會漂** —— 計畫給的 `file:line` 是 2026-05-24 盤點值，執行時以 grep anchor 為準重新定位。

**測試基準**（執行前先量，spec §item 10 載 3b 後 741 unit + 47 integration）：
```bash
cd backend_py && uv run pytest -m "not integration" -q 2>&1 | tail -3
```
3c 後預期：unit 減約 40（6 個純-Axiom unit 測試檔 + 退役 smoke 測試），L3/replay 整合測試改寫或刪。

---

## File Structure（決策鎖定）

**新增**
- `src/bfx_funding_bot/modules/observability/stdout_sink.py` — `StdoutEventSink`：drop-in `async emit(dict)`，合併 `EventResource.envelope_fields()` 後以 JSON 單行寫 stdout logger。無 start/stop/flush（同步寫、無背景 loop）。operational telemetry 的新落點。
- `src/bfx_funding_bot/modules/admin/pg_event_log_query.py` — `PostgresEventLogQueryAdapter`：`async query_order_events(account_id, since)` 查 `event_log`，供 L3 smoke read-your-writes。取代 `AxiomSmokeQueryAdapter`。

**改寫**
- `marketfeed/config.py` — `MarketfeedConfig` 加 `deployment_environment` 欄位，`load_config()` 直接讀 `BFX_DEPLOYMENT_ENV`；刪 `AXIOM_API_KEY`/`AXIOM_DATASET` 欄位與驗證。
- `marketfeed/daemon.py` — wiring 換 `StdoutEventSink`、`PostgresEventLogQueryAdapter`；`deployment_environment` 改用 `config.deployment_environment`；刪 axiom 三段（`_axiom_loop`+sub-task、`AxiomConfig/Client` init+on_flush heartbeat、`AxiomEventSink` 訂閱）。
- operational consumers（`signal_engine.py`/`health_monitor.py`/`fill_tracker.py`/`live_executor.py`/`ws_dispatcher.py`/`paper.py`/`emit.py`）— `_AxiomProtocol`→`_EventSink`、param `axiom`→`event_sink`、`self._axiom`→`self._events`（純 rename，emit 呼叫不變）。
- `safety/chain.py` + `emit.py::emit_safety_trigger` — diagnostics-bound `_AxiomProtocol`→`_DiagnosticsProtocol`（item 10）。
- `smoke_runner.py` — `_poll_axiom_round_trip`→`_poll_pg_event_log_round_trip`、刪 `axiom_client` param、`axiom_query`→`pg_query`。
- 測試 fake `_CaptureAxiom`→`_EventCapture`、`_FakeAxiomQuery`/`_StubAxiomQuery`/`_DummyAxiom` 等隨改。
- 註解/docstring（`bus.py`/`events.py`/`event_upcasters.py`/`ledger.py`/`registry_offers.py`）去 Axiom 用語。

**純刪除（source）**：`external/axiom.py`、`modules/admin/axiom_query.py`、`modules/execution/axiom_event_query.py`、`modules/execution/axiom_sink.py`、`smoke/g1.py`、`scripts/paper_smoke_runner.py`。
**純刪除（test）**：`tests/external/test_axiom.py`、`tests/modules/admin/test_axiom_query.py`、`tests/modules/execution/test_axiom_sink.py`、`tests/modules/execution/test_axiom_event_query.py`、`tests/modules/observability/test_axiom_client_resource.py`、`tests/modules/marketfeed/test_axiom_config.py`、`tests/integration/test_axiom_replay_query_real.py`、G1/paper smoke 對應測試。

**保留不動**：`event_store/`（SoT）、`diagnostics/`（3b）、`boot_recovery.py`（3a-recovery）、`observability/resource.py`（`EventResource` 改由 `StdoutEventSink` 接手）。

---

## Phase A — 建替代物（expand，不動既有 wiring）

### Task 1: `StdoutEventSink`（operational telemetry → structured stdout）

**Files:**
- Create: `src/bfx_funding_bot/modules/observability/stdout_sink.py`
- Test: `tests/modules/observability/test_stdout_sink.py`

設計：與 `AxiomClient` 同 `emit(dict)` port（drop-in），但寫 stdout 而非 SaaS。沿用 `AxiomClient` 對 `EventResource` 的處理（`external/axiom.py` 內有「Must mirror every key `EventResource.envelope_fields()`」的合併邏輯）—— 把 resource envelope fields 併入每筆 event 後 `json.dumps` 成單行。無背景 flush loop（同步寫，故 daemon 不再需要 axiom sub-task）。

- [ ] **Step 1: 先讀 `AxiomClient` 對 resource 的合併邏輯與 `EventResource.envelope_fields()`**

Run:
```bash
cd backend_py
sed -n '88,140p' src/bfx_funding_bot/external/axiom.py
grep -n "def envelope_fields\|def __init__" src/bfx_funding_bot/modules/observability/resource.py
```
目的：確認 `envelope_fields()` 回傳 keys + 合併方向（event 覆蓋 resource，還是 resource 覆蓋 event）。`StdoutEventSink` 必須對齊既有合併方向。

- [ ] **Step 2: 寫 failing test**

`tests/modules/observability/test_stdout_sink.py`：
```python
"""StdoutEventSink: operational telemetry → structured stdout (3c)."""
import json
import logging

import pytest

from bfx_funding_bot.modules.observability.resource import EventResource
from bfx_funding_bot.modules.observability.stdout_sink import StdoutEventSink


def _resource() -> EventResource:
    # mirror daemon.py 的建構參數；deployment_environment 是 envelope_fields 的一員
    return EventResource(deployment_environment="ci")


@pytest.mark.asyncio
async def test_emit_writes_single_json_line_with_envelope_fields(caplog) -> None:
    sink = StdoutEventSink(resource=_resource())
    with caplog.at_level(logging.INFO, logger="bfx_funding_bot.events"):
        await sink.emit({"event_type": "signal", "payload": {"score": 0.42}})

    assert len(caplog.records) == 1
    line = json.loads(caplog.records[0].getMessage())
    assert line["event_type"] == "signal"
    assert line["payload"] == {"score": 0.42}
    # envelope enrichment：deployment_environment 來自 resource
    assert line["deployment_environment"] == "ci"


@pytest.mark.asyncio
async def test_event_fields_win_over_resource(caplog) -> None:
    # event 自帶的 key 不可被 resource 覆蓋（drop-in 對齊 AxiomClient 合併方向）
    sink = StdoutEventSink(resource=_resource())
    with caplog.at_level(logging.INFO, logger="bfx_funding_bot.events"):
        await sink.emit({"event_type": "x", "deployment_environment": "shadow"})
    line = json.loads(caplog.records[0].getMessage())
    assert line["deployment_environment"] == "shadow"


@pytest.mark.asyncio
async def test_emit_is_lossless_and_json_safe(caplog) -> None:
    # 含 Decimal/datetime 等非原生 JSON 型別不可炸（default=str）
    from datetime import UTC, datetime
    from decimal import Decimal

    sink = StdoutEventSink(resource=_resource())
    with caplog.at_level(logging.INFO, logger="bfx_funding_bot.events"):
        await sink.emit({"amt": Decimal("1.5"), "ts": datetime(2026, 5, 24, tzinfo=UTC)})
    line = json.loads(caplog.records[0].getMessage())
    assert line["amt"] == "1.5"
```
> 註：Step 1 若發現合併方向相反（resource 覆蓋 event），把 `test_event_fields_win_over_resource` 的斷言與實作一起反向；以 `AxiomClient` 既有行為為準。

- [ ] **Step 3: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/modules/observability/test_stdout_sink.py -q`
Expected: FAIL（`ModuleNotFoundError: stdout_sink`）

- [ ] **Step 4: 實作 `StdoutEventSink`**

`src/bfx_funding_bot/modules/observability/stdout_sink.py`：
```python
"""StdoutEventSink — operational telemetry sink (3c, replaces AxiomClient).

Drop-in for the `emit(dict)` port shared with the (removed) AxiomClient and the
DiagnosticsSink. Writes one JSON line per event to a dedicated stdout logger so
the deploy platform (Koyeb) can route/retain it. No background flush loop:
emit() is synchronous, so the daemon no longer needs an "axiom" sub-task.

Operational events only (SIGNAL / HEALTH_CHECK / ORDER_SUBMIT / lifecycle).
Forensic events go to DiagnosticsSink (PG); SoT events go to event_log.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from bfx_funding_bot.modules.observability.resource import EventResource

log = logging.getLogger("bfx_funding_bot.events")


class StdoutEventSink:
    """Structured-stdout operational event sink."""

    def __init__(self, *, resource: EventResource) -> None:
        self._resource = resource

    async def emit(self, event: dict[str, Any]) -> None:
        # resource envelope fields as defaults; event-supplied keys win (mirror
        # AxiomClient merge direction — verify in Step 1).
        enriched = {**self._resource.envelope_fields(), **event}
        log.info(json.dumps(enriched, default=str))
```
> 若 `EventResource()` 建構簽章與 test 不符（Step 1 確認），同步修正 test 與此處的建構參數。

- [ ] **Step 5: 跑測試確認 pass**

Run: `cd backend_py && uv run pytest tests/modules/observability/test_stdout_sink.py -q && uv run mypy src/bfx_funding_bot/modules/observability/stdout_sink.py && uv run ruff check src/bfx_funding_bot/modules/observability/stdout_sink.py`
Expected: PASS + clean

- [ ] **Step 6: Commit**
```bash
git add src/bfx_funding_bot/modules/observability/stdout_sink.py tests/modules/observability/test_stdout_sink.py
git commit -m "✨ Feat: add StdoutEventSink (operational telemetry → structured stdout, 3c T1)"
```

---

### Task 2: `deployment_environment` 從 `AxiomConfig` 解耦 → `load_config()`

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/config.py`（`MarketfeedConfig` + `load_config`）
- Test: `tests/modules/marketfeed/test_config.py`（既有，加 case；若不存在則查 `test_config*.py` 實際檔名）

目標：`BFX_DEPLOYMENT_ENV` 由 `load_config()` 直接讀並放進 `MarketfeedConfig.deployment_environment`，不再經 `AxiomConfig.from_env()`。先讓 config 提供，daemon 改用在 Task 7（避免一次動太多）。

- [ ] **Step 1: 確認既有讀法 pattern + DeploymentEnvironment 型別**

Run:
```bash
cd backend_py
grep -n "BFX_PHASE\|DeploymentEnvironment\|deployment_env\|class MarketfeedConfig\|def load_config" src/bfx_funding_bot/modules/marketfeed/config.py
grep -rn "class DeploymentEnvironment" src/
ls tests/modules/marketfeed/test_config*.py 2>/dev/null
```
記下 `MarketfeedConfig` 是 dataclass/pydantic、`DeploymentEnvironment` enum 的 import 路徑、`load_config` 讀 `BFX_PHASE` 的確切寫法（要對齊）。

- [ ] **Step 2: 寫 failing test**

於 marketfeed config 測試檔加（檔名依 Step 1）：
```python
def test_load_config_reads_deployment_environment(monkeypatch, _min_env):
    # _min_env: 既有 fixture 設好 BFX_PHASE / DATABASE_URL 等必要 env；若無則 inline set
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "shadow")
    cfg = load_config()
    assert cfg.deployment_environment == DeploymentEnvironment.SHADOW


def test_load_config_missing_deployment_environment_raises(monkeypatch, _min_env):
    monkeypatch.delenv("BFX_DEPLOYMENT_ENV", raising=False)
    with pytest.raises(ValueError, match="BFX_DEPLOYMENT_ENV"):
        load_config()
```
> 對齊既有 test_axiom_config.py 對 `BFX_DEPLOYMENT_ENV` 的驗證語意（該檔 Task 11 會刪，此處承接其 deployment_env 驗證責任）。

- [ ] **Step 3: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_config*.py -q -k deployment_environment`
Expected: FAIL（`MarketfeedConfig` 無此欄位）

- [ ] **Step 4: 實作**

`config.py`：
1. import `DeploymentEnvironment`（路徑依 Step 1）。
2. `MarketfeedConfig` 加欄位 `deployment_environment: DeploymentEnvironment`。
3. `load_config()` 內，仿 `BFX_PHASE` 讀法新增：
```python
deployment_env_str = os.environ.get("BFX_DEPLOYMENT_ENV", "").strip()
if not deployment_env_str:
    raise ValueError("BFX_DEPLOYMENT_ENV required (one of: prod, shadow, ci)")
deployment_environment = DeploymentEnvironment(deployment_env_str)
```
4. 建構 `MarketfeedConfig(...)` 時帶入 `deployment_environment=deployment_environment`。
> 本 task **先不刪** `axiom_api_key`/`axiom_dataset` 欄位（daemon 仍在用，Task 14 才刪），只 **新增** deployment_environment。

- [ ] **Step 5: 跑測試確認 pass**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/ -q && uv run mypy src/bfx_funding_bot/modules/marketfeed/config.py`
Expected: PASS

- [ ] **Step 6: Commit**
```bash
git add src/bfx_funding_bot/modules/marketfeed/config.py tests/modules/marketfeed/
git commit -m "✨ Feat: load deployment_environment in load_config, decouple from AxiomConfig (3c T2)"
```

---

### Task 3: `PostgresEventLogQueryAdapter`（L3 smoke 改查 PG）

**Files:**
- Create: `src/bfx_funding_bot/modules/admin/pg_event_log_query.py`
- Test: `tests/integration/test_pg_event_log_query.py`（標 `@pytest.mark.integration`，需 PG）

實作與 `AxiomSmokeQueryAdapter.query_order_events(account_id, since)` 同簽章（drop-in），查 `event_log` 取代 Axiom APL。回傳 `list[dict]`，含 smoke 斷言要用的 `event_type`。事件型別過濾用 `RESERVATION_CLAIMED`/`ORDER_FILL`/`RESERVATION_RELEASED`（與舊 APL 對齊；這三型都在 event_log）。

- [ ] **Step 1: 確認 event_log 欄位 + 既有 SELECT pattern + session_factory 型別**

Run:
```bash
cd backend_py
grep -n "class EventLogRow\|event_type\|occurred_at_ms\|account_id\|deployment_environment" src/bfx_funding_bot/modules/execution/event_store/tables.py
grep -n "select(EventLogRow\|pg_session_factory\|async_sessionmaker\|session_factory" tests/integration/test_pg_event_store.py | head
```
記下：`EventLogRow` 欄位（`event_type`/`account_id`/`deployment_environment`/`occurred_at_ms`/`payload`/`event_seq`）、time 欄位是 `occurred_at_ms`(int ms) 還是 `recorded_at`(datetime)、測試 session_factory fixture 名。

- [ ] **Step 2: 寫 failing integration test**

`tests/integration/test_pg_event_log_query.py`：
```python
"""PostgresEventLogQueryAdapter — L3 smoke read-your-writes (3c)."""
from datetime import UTC, datetime, timedelta

import pytest

from bfx_funding_bot.modules.admin.pg_event_log_query import PostgresEventLogQueryAdapter
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
# 事件型別/建構依 test_pg_event_store.py 既有 import（ReservationClaimed / OrderFilled）

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_query_returns_claimed_and_fill_for_account(pg_session_factory) -> None:
    env = "ci"
    acct = "SMOKE"
    store = PostgresEventStore(deployment_environment=env)
    since = datetime.now(UTC) - timedelta(minutes=1)
    async with pg_session_factory() as s:
        # 用與 test_pg_event_store.py 相同的事件建構（CLAIMED + FILL）
        await store.append(s, _claimed(account_id=acct))
        await store.append(s, _filled(account_id=acct))
        await s.commit()

    adapter = PostgresEventLogQueryAdapter(
        session_factory=pg_session_factory, deployment_environment=env
    )
    rows = await adapter.query_order_events(acct, since)
    types = {r["event_type"] for r in rows}
    assert "RESERVATION_CLAIMED" in types
    assert "ORDER_FILL" in types


@pytest.mark.asyncio
async def test_query_scopes_by_account_and_env(pg_session_factory) -> None:
    # 別的 account / env 的事件不可外洩
    ...
```
> `_claimed`/`_filled` helper 直接照抄 `test_pg_event_store.py` 的事件建構（避免重發明）；執行時把實際 import/建構補上。

- [ ] **Step 3: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/integration/test_pg_event_log_query.py -q`
Expected: FAIL（module 不存在）

- [ ] **Step 4: 實作 adapter**

`src/bfx_funding_bot/modules/admin/pg_event_log_query.py`：
```python
"""PostgresEventLogQueryAdapter — query event_log for L3 smoke (3c).

Replaces AxiomSmokeQueryAdapter. Same `query_order_events(account_id, since)`
port; reads PG event_log (read-your-writes) instead of Axiom APL round-trip.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

_ORDER_EVENT_TYPES = ("RESERVATION_CLAIMED", "ORDER_FILL", "RESERVATION_RELEASED")


class PostgresEventLogQueryAdapter:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        deployment_environment: str,
    ) -> None:
        self._sf = session_factory
        self._env = deployment_environment

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        since_ms = int(since.timestamp() * 1000)
        stmt = (
            select(EventLogRow)
            .where(
                EventLogRow.account_id == account_id,
                EventLogRow.deployment_environment == self._env,
                EventLogRow.event_type.in_(_ORDER_EVENT_TYPES),
                EventLogRow.occurred_at_ms >= since_ms,
            )
            .order_by(EventLogRow.occurred_at_ms.asc())
        )
        async with self._sf() as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [
            {
                "event_type": r.event_type,
                "account_id": r.account_id,
                "occurred_at_ms": r.occurred_at_ms,
            }
            for r in rows
        ]
```
> `session_factory` / `AsyncSession` import 路徑、`occurred_at_ms` vs `recorded_at` 依 Step 1 校正。

- [ ] **Step 5: 跑測試確認 pass**

Run: `cd backend_py && uv run pytest tests/integration/test_pg_event_log_query.py -q && uv run mypy src/bfx_funding_bot/modules/admin/pg_event_log_query.py && uv run ruff check src/bfx_funding_bot/modules/admin/pg_event_log_query.py`
Expected: PASS + clean（無 PG 環境則 integration skip，至少 mypy/ruff 過）

- [ ] **Step 6: Commit**
```bash
git add src/bfx_funding_bot/modules/admin/pg_event_log_query.py tests/integration/test_pg_event_log_query.py
git commit -m "✨ Feat: PostgresEventLogQueryAdapter for L3 smoke read-your-writes (3c T3)"
```

---

## Phase B — Repoint consumers（swap off AxiomClient，AxiomClient 仍在）

### Task 4: operational consumers repoint → `StdoutEventSink`（rename `_AxiomProtocol`→`_EventSink`）

純機械 rename + wiring swap，**emit 呼叫點完全不變**（同 `emit(dict)` port）。涉及檔案多，逐檔改、改完一檔跑該檔測試。

**Files（每檔：`_AxiomProtocol`→`_EventSink`、param `axiom`→`event_sink`、`self._axiom`/`self.axiom`→`self._events`）:**
- `src/bfx_funding_bot/modules/marketfeed/signal_engine.py`（注意：保留 `diagnostics` param，只改 `axiom` 那條 + 其 `_AxiomProtocol`）
- `src/bfx_funding_bot/modules/marketfeed/health_monitor.py`
- `src/bfx_funding_bot/external/bitfinex/fill_tracker.py`
- `src/bfx_funding_bot/external/bitfinex/live_executor.py`
- `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py`
- `src/bfx_funding_bot/modules/execution/paper.py`
- `src/bfx_funding_bot/modules/execution/emit.py`（`emit_order_submit`/`emit_order_fill` 的 `axiom` param → `event_sink`；**`emit_safety_trigger` 不動**，留給 Task 5）

**Test fakes（`_CaptureAxiom`→`_EventCapture`，含 instantiation `axiom=...`→`event_sink=...`）:**
- `tests/modules/execution/test_emit.py`、`tests/modules/execution/test_paper.py`
- `tests/external/bitfinex/test_fill_tracker.py`、`test_fill_tracker_registry_aware.py`（`_DummyAxiom`）、`test_integration_fill_tracker.py`
- `tests/modules/marketfeed/conftest.py`（fixture）
- `tests/integration/phase4_4a/conftest.py`、`tests/modules/execution/test_integration_path_a.py`、`test_integration_path_b.py`

- [ ] **Step 1: 列出所有 `_AxiomProtocol` 定義與 `axiom` param/attr 的確切位置**

Run:
```bash
cd backend_py
grep -rn "_AxiomProtocol\|axiom:\|self\._axiom\|self\.axiom\|axiom=" src/bfx_funding_bot/modules/marketfeed/signal_engine.py src/bfx_funding_bot/modules/marketfeed/health_monitor.py src/bfx_funding_bot/external/bitfinex/fill_tracker.py src/bfx_funding_bot/external/bitfinex/live_executor.py src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py src/bfx_funding_bot/modules/execution/paper.py src/bfx_funding_bot/modules/execution/emit.py
```
逐一比對，確保不誤改 `signal_engine` 的 `diagnostics` param、不誤改 `emit_safety_trigger`。

- [ ] **Step 2: 逐檔 rename（source）**

每檔做三件事（emit 呼叫 body 不動）：
- `class _AxiomProtocol(Protocol): async def emit(self, event: dict[str, Any]) -> None: ...` → `class _EventSink(Protocol): ...`
- 建構子 param `axiom: _AxiomProtocol` → `event_sink: _EventSink`（`emit.py` 的 free function 同理）
- attr `self._axiom = axiom`/`self.axiom = axiom` → `self._events = event_sink`，並把 method 內 `self._axiom.emit(...)`/`self.axiom.emit(...)` → `self._events.emit(...)`

`emit.py::emit_order_submit`/`emit_order_fill` 的 `axiom` 參數同改 `event_sink`；呼叫處（`paper.py`/`live_executor.py`）傳參改名。
> `signal_engine.py`：只動 `axiom` 那條（→ operational SIGNAL/SIGNAL_DIVERGENCE/HEALTH_CHECK）。`diagnostics`（DECISION）留到 Task 5 改 protocol 名。

- [ ] **Step 3: 逐檔 rename（test fakes）**

各測試檔 `class _CaptureAxiom`（及 `_DummyAxiom`）→ `class _EventCapture`；instantiation `axiom=_CaptureAxiom()` → `event_sink=_EventCapture()`（依該檔被測對象的新 param 名）。

- [ ] **Step 4: 跑受影響測試**

Run:
```bash
cd backend_py && uv run pytest tests/modules/marketfeed/ tests/external/bitfinex/ tests/modules/execution/test_emit.py tests/modules/execution/test_paper.py tests/modules/execution/test_integration_path_a.py tests/modules/execution/test_integration_path_b.py -q
```
Expected: PASS（仍傳 AxiomClient 進去也 OK —— port 相容；本 task 不改 daemon wiring）

- [ ] **Step 5: daemon wiring 換 `StdoutEventSink`**

`daemon.py`：在 `event_resource = EventResource(...)` 後新增 `stdout_sink = StdoutEventSink(resource=event_resource)`（import StdoutEventSink）；把原本傳 `axiom=axiom` 給 signal_engine/health_monitor/fill_tracker/ws_dispatcher/live_executor/paper executor 的地方改傳 `event_sink=stdout_sink`。
> `AxiomClient` 此時**仍存活**（smoke_runner Task 6、AxiomEventSink Task 8 還在用），只是 operational consumers 不再用它。daemon 內 `await axiom.emit({...})`（`_emit_locf_degraded`、replay_floor_hit_count HEALTH_CHECK，約 daemon.py:564/882）也改 `await stdout_sink.emit({...})`。

- [ ] **Step 6: 跑 daemon wiring 測試 + 全 unit**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/ -q && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS + clean（`test_daemon_axiom_wiring.py` 可能需小調 —— 若它斷言 axiom 被傳給這些 consumer，改斷言 stdout_sink；此檔 Task 10 重整或刪）

- [ ] **Step 7: Commit**
```bash
git add -A
git commit -m "♻️ Refactor: repoint operational consumers to StdoutEventSink, rename _AxiomProtocol→_EventSink (3c T4)"
```

---

### Task 5: diagnostics-bound `_AxiomProtocol`→`_DiagnosticsProtocol`（item 10 follow-up）

**Files:**
- `src/bfx_funding_bot/modules/execution/safety/chain.py`（`_AxiomProtocol`→`_DiagnosticsProtocol`，param 已是 `diagnostics`，不改 param 名）
- `src/bfx_funding_bot/modules/execution/emit.py`（`emit_safety_trigger` 的 `diagnostics: _AxiomProtocol`→`_DiagnosticsProtocol`）
- `src/bfx_funding_bot/modules/marketfeed/signal_engine.py`（`diagnostics` param 的 protocol annotation→`_DiagnosticsProtocol`）
- Test: `tests/modules/execution/safety/test_chain.py`（`_CaptureAxiom`→`_EventCapture` 若尚未在 T4 改）

- [ ] **Step 1: 定位 diagnostics-bound protocol**

Run: `cd backend_py && grep -rn "_AxiomProtocol\|diagnostics:" src/bfx_funding_bot/modules/execution/safety/chain.py src/bfx_funding_bot/modules/execution/emit.py src/bfx_funding_bot/modules/marketfeed/signal_engine.py`

- [ ] **Step 2: rename `_AxiomProtocol`→`_DiagnosticsProtocol`** 於上述三檔的 diagnostics annotation。logic 不動。

- [ ] **Step 3: 跑測試**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/ tests/modules/marketfeed/ -q && uv run mypy src/ && uv run ruff check`
Expected: PASS + clean

- [ ] **Step 4: Commit**
```bash
git add -A
git commit -m "♻️ Refactor: rename diagnostics-bound _AxiomProtocol→_DiagnosticsProtocol (3c T5, item 10)"
```

---

### Task 6: L3 smoke repoint → `PostgresEventLogQueryAdapter`

**Files:**
- Modify: `src/bfx_funding_bot/modules/admin/smoke_runner.py`
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`（smoke wiring，約 daemon.py:858-874）
- Test: `tests/modules/admin/test_smoke_runner.py`（依實際檔名）

改造：`_poll_axiom_round_trip`→`_poll_pg_event_log_round_trip`（poll 邏輯/次數/間隔不變，只換 query backend）；刪 `axiom_client` param（L3 事件經 executor→EventStorePersister 落 event_log，不再靠 axiom emit）；`axiom_query`→`pg_query`，型別 `_AxiomQueryProtocol`→`_EventLogQueryProtocol`。

- [ ] **Step 1: 讀 smoke_runner 現況**

Run:
```bash
cd backend_py
sed -n '50,110p' src/bfx_funding_bot/modules/admin/smoke_runner.py   # protocols + __init__
grep -n "_poll_axiom_round_trip\|_axiom\|axiom_client\|axiom_query\|_run_l3" src/bfx_funding_bot/modules/admin/smoke_runner.py
ls tests/modules/admin/test_smoke_runner*.py
```
確認 L3 流程：執行 chain → 事件上 bus → persister 落 event_log → poll。確認 `axiom_client` 在 L3 路徑除了 emit 還有沒有別的用途（若無，可純刪）。

- [ ] **Step 2: 改測試（先紅）**

`test_smoke_runner.py`：把 L3 測試的 `axiom_query` fake（查 events）改成新的 `pg_query` fake（同回傳 `[{"event_type": ...}]`）、移除 `axiom_client` 建構參數。fake 名 `_StubAxiomQuery`→`_StubEventLogQuery`。斷言 checks key `axiom_events_seen`/`axiom_event_types`→`pg_events_seen`/`pg_event_types`。

Run: `cd backend_py && uv run pytest tests/modules/admin/test_smoke_runner*.py -q`
Expected: FAIL

- [ ] **Step 3: 改 smoke_runner**
- `_AxiomQueryProtocol`→`_EventLogQueryProtocol`（簽章不變：`query_order_events(account_id, since)`）
- `__init__` 刪 `axiom_client: _AxiomProtocol` param 與 `self._axiom`；`axiom_query`→`pg_query`、`self._axiom_query`→`self._pg_query`
- `_poll_axiom_round_trip`→`_poll_pg_event_log_round_trip`：body 內 `self._axiom_query.query_order_events`→`self._pg_query.query_order_events`，checks key 改 `pg_events_seen`/`pg_event_types`，error message `axiom round-trip timeout`→`pg event_log round-trip timeout`
- `_run_l3_unlocked` 內 docstring/呼叫對應更新
- 刪 `_AxiomProtocol`（smoke_runner 內那個，若 axiom_client 移除後無用）

- [ ] **Step 4: 改 daemon smoke wiring**

`daemon.py`（約 858-874）：
```python
# 刪：
#   from bfx_funding_bot.modules.admin.axiom_query import AxiomSmokeQueryAdapter
#   smoke_axiom_query = AxiomSmokeQueryAdapter(api_key=config.axiom_api_key, dataset=config.axiom_dataset)
# 改：
from bfx_funding_bot.modules.admin.pg_event_log_query import PostgresEventLogQueryAdapter
smoke_pg_query = PostgresEventLogQueryAdapter(
    session_factory=session_factory,        # daemon 既有的 PG session factory（grep 確認變數名）
    deployment_environment=env_str,
)
smoke_runner = SmokeRunner(
    executor=wrapped_executor,
    bus=bus,
    pg_query=smoke_pg_query,                 # 不再傳 axiom_client
    phase=config.phase,
    strategy=first_cell.strategy,
    cell=first_cell.cell_id,
)
```
> `session_factory` 變數名 grep daemon 確認（3a/3b 已有 PG session factory 給 persister/diagnostics 用，沿用同一個）。

- [ ] **Step 5: 跑測試 + 整合 smoke**

Run:
```bash
cd backend_py
uv run pytest tests/modules/admin/ -q
uv run pytest -m "not integration" -q
uv run mypy src/ && uv run ruff check
```
Expected: PASS + clean

- [ ] **Step 6: Commit**
```bash
git add -A
git commit -m "♻️ Refactor: L3 smoke poll PG event_log instead of Axiom round-trip (3c T6)"
```

---

## Phase C — daemon deployment_env 改用 config

### Task 7: daemon 改用 `config.deployment_environment`

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`

把所有 `axiom_cfg.deployment_env` / `env_str = axiom_cfg.deployment_env.value` 改成從 `config.deployment_environment` 取（Task 2 已備）。此 task 後 `AxiomConfig` 在 daemon 只剩 `AxiomClient` 自己用（api_key/dataset），deployment_env 不再依賴它。

- [ ] **Step 1: 定位**

Run: `cd backend_py && grep -n "axiom_cfg\.deployment_env\|env_str\s*=\|deployment_environment=" src/bfx_funding_bot/modules/marketfeed/daemon.py`

- [ ] **Step 2: 改寫**

`daemon.py`：
```python
# 新增（build_daemon 取得 config 後）：
env = config.deployment_environment
env_str = env.value
# EventResource / PostgresEventStore / DiagnosticsSink / ledger / registry / smoke 全改用 env / env_str
# axiom_cfg.deployment_env 的引用全數替換
```
`AxiomConfig.from_env()` 暫時保留（`AxiomClient` 仍需 api_key/dataset，Task 10 才刪）；只把 deployment_env 來源換掉。

- [ ] **Step 3: 跑測試**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS + clean

- [ ] **Step 4: Commit**
```bash
git add -A
git commit -m "♻️ Refactor: daemon uses config.deployment_environment, not AxiomConfig (3c T7)"
```

---

## Phase D — 刪 Axiom（contract）

### Task 8: 刪 `AxiomEventSink` + bus 訂閱

**Files:**
- Delete: `src/bfx_funding_bot/modules/execution/axiom_sink.py`
- Delete: `tests/modules/execution/test_axiom_sink.py`
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`（刪 import + 實例化 + 三條 `bus.subscribe`，約 daemon.py:58/825-844）
- Modify: `src/bfx_funding_bot/modules/execution/bus.py`（去 axiom_sink 註解，約 bus.py:96）

驗證刪除安全：`AxiomEventSink.on_reservation_*` 的 emit 是冗餘（event_log 才是 SoT）；`handle_cancel_*` 已 unsubscribe。刪後 reservation lifecycle 的持久化完全靠 EventStorePersister，無遺漏。

- [ ] **Step 1: 確認無其他 import**

Run: `cd backend_py && grep -rn "axiom_sink\|AxiomEventSink" src/ tests/ | grep -v __pycache__`
Expected: 只剩 daemon.py + axiom_sink.py + test_axiom_sink.py

- [ ] **Step 2: 刪 daemon wiring**

`daemon.py`：刪 `from ...axiom_sink import AxiomEventSink`（:58）、`axiom_sink = AxiomEventSink(...)`（:825-830）、`bus.subscribe(ReservationClaimed/OrderFilled/ReservationReleased, axiom_sink.*)`（:834-836）、obsolete 註解（:842-844）。`bus.py` 去 axiom_sink 字樣註解。

- [ ] **Step 3: 刪檔**

Run: `cd backend_py && git rm src/bfx_funding_bot/modules/execution/axiom_sink.py tests/modules/execution/test_axiom_sink.py`

- [ ] **Step 4: 驗證**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS + clean

- [ ] **Step 5: Commit**
```bash
git add -A
git commit -m "🔥 Remove: AxiomEventSink + bus subscriptions (redundant SoT emit; event_log is SoT) (3c T8)"
```

---

### Task 9: 刪 replay 路徑（`AxiomReplayQueryAdapter` + `replay_from_axiom` + `_AxiomQueryProtocol`）

**Files:**
- Delete: `src/bfx_funding_bot/modules/execution/axiom_event_query.py`
- Delete: `tests/modules/execution/test_axiom_event_query.py`
- Modify: `src/bfx_funding_bot/modules/execution/ledger.py`（刪 `_AxiomQueryProtocol` + `replay_from_axiom()`，約 :50-53/:151-224；docstring 去 Axiom）
- Modify: `src/bfx_funding_bot/modules/execution/registry_offers.py`（刪 `_AxiomQueryProtocol` + `replay_from_axiom()` + `__init__` 的 `axiom_query` param + `from_snapshot` 內 `reg._axiom_query = None` shim，約 :187-201/:212-220/:238-253/:275-276）
- Test: `tests/modules/execution/test_ledger.py`（`_FakeAxiomQuery`→刪/改）、`tests/integration/phase4_4a/conftest.py`（`StubAxiomQuery`）

- [ ] **Step 1: 確認 replay 全 dead（boot 走 from_snapshot）**

Run:
```bash
cd backend_py
grep -rn "replay_from_axiom\|AxiomReplayQueryAdapter\|_axiom_query\|axiom_query" src/ tests/ | grep -v __pycache__ | grep -v "smoke_runner\|pg_event_log"
```
確認 `replay_from_axiom` 無生產呼叫（只測試引用）、`axiom_query` 在 registry_offers 是 dead param。

- [ ] **Step 2: 刪 ledger replay**

`ledger.py`：刪 `_AxiomQueryProtocol`、`replay_from_axiom()`；docstring「Axiom is SoT」「Axiom replay」→「PostgreSQL event_log is SoT」。保留 `from_snapshot`（live）。

- [ ] **Step 3: 刪 registry_offers replay + axiom_query param**

`registry_offers.py`：刪 `_AxiomQueryProtocol`、`replay_from_axiom()`、`__init__` 的 `axiom_query` param 與 attr、`from_snapshot` 內 `reg._axiom_query = None` shim。grep daemon 確認 `OfferRegistry(` 建構處沒傳 `axiom_query`（3a 後應已不傳；若有則一併刪）。

- [ ] **Step 4: 刪檔 + 改測試**

Run: `cd backend_py && git rm src/bfx_funding_bot/modules/execution/axiom_event_query.py tests/modules/execution/test_axiom_event_query.py`
改 `test_ledger.py`：刪用到 `replay_from_axiom`/`_FakeAxiomQuery` 的測試 case（這些測 dead 功能），保留 from_snapshot 測試。`phase4_4a/conftest.py` 的 `StubAxiomQuery` 若無人用則刪。

- [ ] **Step 5: 驗證**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS + clean

- [ ] **Step 6: Commit**
```bash
git add -A
git commit -m "🔥 Remove: Axiom replay path (adapter + replay_from_axiom + _AxiomQueryProtocol; boot uses from_snapshot) (3c T9)"
```

---

### Task 10: 刪 `AxiomClient`/`AxiomConfig` + daemon axiom loop/heartbeat

**Files:**
- Delete: `src/bfx_funding_bot/external/axiom.py`
- Delete: `tests/external/test_axiom.py`、`tests/modules/observability/test_axiom_client_resource.py`、`tests/modules/marketfeed/test_axiom_config.py`
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`（刪 axiom import :34-37、`axiom: AxiomClient` 欄位 :153、`_axiom_loop` sub-task :202、`_axiom_loop` method :274-284、`AxiomConfig.from_env()`+`AxiomClient` init+on_flush :620-628、`flush axiom` 註解 :6）
- Modify: `src/bfx_funding_bot/modules/marketfeed/health_monitor.py`（刪 `SUB_TASK_THRESHOLDS` 的 `"axiom": 60`，:36）
- Modify: `tests/modules/marketfeed/test_daemon_axiom_wiring.py`（重整為 stdout/PG wiring 驗證，或刪）

此 task 後 Axiom client 徹底消失。`EventResource` 保留（`StdoutEventSink` 用）。`StdoutEventSink` 同步寫、無背景 loop，故 axiom sub-task + heartbeat 整條移除無 liveness 缺口。

- [ ] **Step 1: 確認 AxiomClient/Config 只剩自身 + daemon 引用**

Run: `cd backend_py && grep -rn "AxiomClient\|AxiomConfig\|AxiomAuthError\|from_env\|_axiom_loop\|on_flush" src/ tests/ | grep -v __pycache__`
Expected: 只 axiom.py + daemon.py + 三個 axiom 測試檔

- [ ] **Step 2: 移除 daemon axiom 三段**
- import（:34-37）、`axiom: AxiomClient` dataclass 欄位（:153）
- `tg.create_task(self._axiom_loop(), name="axiom")`（:202）+ `_axiom_loop` method（:274-284）
- `axiom_cfg = AxiomConfig.from_env()` / `axiom_cfg.on_flush = ...` / `axiom = AxiomClient(...)`（:620-628）—— 注意 `event_resource = EventResource(...)` **保留**（給 stdout_sink）
- module docstring「flush axiom」字樣（:6）→「flush stdout sink (noop)」或刪

- [ ] **Step 3: 移除 heartbeat axiom 條目**

`health_monitor.py:36`：刪 `"axiom": 60,` 該行（含註解）。grep 確認沒有測試硬斷言 `"axiom"` 在 `SUB_TASK_THRESHOLDS`。

- [ ] **Step 4: 刪檔 + 處理 wiring 測試**

Run: `cd backend_py && git rm src/bfx_funding_bot/external/axiom.py tests/external/test_axiom.py tests/modules/observability/test_axiom_client_resource.py tests/modules/marketfeed/test_axiom_config.py`
`test_daemon_axiom_wiring.py`：原驗證 axiom client 接線 —— 改名/重寫為驗 stdout_sink 接到 consumers + PG event_store 接到 persister，或若已被 Task 4/其他測試涵蓋則 `git rm`。

- [ ] **Step 5: 驗證（含 daemon 啟動路徑）**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS + clean。特別確認無殘留 `import` 錯誤、daemon build 測試綠。

- [ ] **Step 6: Commit**
```bash
git add -A
git commit -m "🔥 Remove: AxiomClient/AxiomConfig + daemon axiom loop & heartbeat (3c T10)"
```

---

### Task 11: 刪 `AxiomSmokeQueryAdapter`

**Files:**
- Delete: `src/bfx_funding_bot/modules/admin/axiom_query.py`
- Delete: `tests/modules/admin/test_axiom_query.py`

- [ ] **Step 1: 確認無引用**（Task 6 已 repoint smoke）

Run: `cd backend_py && grep -rn "axiom_query\|AxiomSmokeQueryAdapter" src/ tests/ | grep -v __pycache__ | grep -v pg_event_log`
Expected: 只剩 axiom_query.py + test_axiom_query.py

- [ ] **Step 2: 刪檔**

Run: `cd backend_py && git rm src/bfx_funding_bot/modules/admin/axiom_query.py tests/modules/admin/test_axiom_query.py`

- [ ] **Step 3: 驗證**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS + clean

- [ ] **Step 4: Commit**
```bash
git add -A
git commit -m "🔥 Remove: AxiomSmokeQueryAdapter (replaced by PostgresEventLogQueryAdapter) (3c T11)"
```

---

### Task 12: 刪 `test_axiom_replay_query_real.py`（PG read-your-writes 已由 test_pg_event_store 涵蓋）

**Files:**
- Delete: `tests/integration/test_axiom_replay_query_real.py`

理由：該檔測 `AxiomReplayQueryAdapter`（Task 9 已刪）的 round-trip；其驗證的事件型別（CLAIMED/FILL/RELEASED）的 PG read-your-writes 已由 `tests/integration/test_pg_event_store.py` + 新 `test_pg_event_log_query.py`（Task 3）涵蓋。§8「改寫 T12」目標已由這兩個整合測試達成，原檔純刪不留缺口。

- [ ] **Step 1: 確認涵蓋**

Run: `cd backend_py && grep -n "read.your.writes\|append\|select(EventLogRow\|deployment_environment" tests/integration/test_pg_event_store.py tests/integration/test_pg_event_log_query.py | head`
確認 PG append→query 斷言已存在。

- [ ] **Step 2: 刪檔**

Run: `cd backend_py && git rm tests/integration/test_axiom_replay_query_real.py`

- [ ] **Step 3: 驗證**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run ruff check`
Expected: PASS（integration 需 PG 才跑，至少 collection 無 import 錯）

- [ ] **Step 4: Commit**
```bash
git add -A
git commit -m "🔥 Remove: test_axiom_replay_query_real (PG read-your-writes covered by test_pg_event_store) (3c T12)"
```

---

### Task 13: 退役 G1 + paper_smoke_runner（使用者決策：整個刪）

**Files:**
- Delete: `src/bfx_funding_bot/smoke/g1.py`（+ `smoke/` 內其他純 G1 檔，如 `g1_smoke_check.py`/`g1_smoke_eda_ranges.py` 若存在）
- Delete: `src/bfx_funding_bot/scripts/paper_smoke_runner.py`
- Delete: 對應測試（`tests/scripts/test_paper_smoke_runner.py`、`tests/smoke/test_g1*.py` 等）
- Modify: `docs/deploy/koyeb-paper.md`（移除 G1/paper smoke 步驟，說明改用 PG-backed L3 smoke endpoint）

- [ ] **Step 1: 盤點 smoke/ 與 scripts/ 的 G1 檔群 + 測試 + 引用**

Run:
```bash
cd backend_py
ls src/bfx_funding_bot/smoke/
grep -rln "g1\|paper_smoke_runner\|AxiomQueryClient\|_query_axiom_signal_count" src/ tests/ | grep -v __pycache__
grep -rn "g1\|paper_smoke\|smoke_check" ../docs/deploy/koyeb-paper.md
```
列出所有要刪的檔 + docs 要改的段落。

- [ ] **Step 2: 刪 source + test**

Run: `cd backend_py && git rm src/bfx_funding_bot/smoke/g1.py src/bfx_funding_bot/scripts/paper_smoke_runner.py <其餘 G1 檔> <對應測試檔>`
（依 Step 1 結果補檔名；若 `smoke/` 清空則一併刪 `__init__.py`/空目錄。）

- [ ] **Step 3: 更新 docs/deploy/koyeb-paper.md**

移除「G1 continuity gate」「`uv run python scripts/g1_smoke_check.py`」「paper_smoke_runner」相關步驟；補一句：3c 起 signal 走 structured stdout（Koyeb logs），部署前驗證改呼叫 PG-backed L3 smoke endpoint `POST /smoke-test?level=L3`。連結指回本 plan。

- [ ] **Step 4: 驗證**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS + clean

- [ ] **Step 5: Commit**
```bash
git add -A
git commit -m "🔥 Remove: retire G1 gate + paper_smoke_runner (signal→stdout, no queryable backend) (3c T13)"
```

---

## Phase E — env / CI 清理

### Task 14: 刪 `AXIOM_API_KEY`/`AXIOM_DATASET`（config + CI + .env）

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/config.py`（刪 `axiom_api_key`/`axiom_dataset` 欄位 + `load_config` 內讀取/驗證，約 :89-90/:115-120/:229-230）
- Modify: `.github/workflows/ci.yml`（刪 `AXIOM_API_KEY`/`AXIOM_DATASET` secrets，約 :114-115；改 :109 註解；:126 integration step 保留）
- Modify: `.env` / `.env.example`（若有 AXIOM_* 則刪；盤點顯示 `.env.example` 無，仍 grep 確認）

此時無任何 code 讀 axiom env（Task 6 smoke、Task 10 client 已移除）。

- [ ] **Step 1: 確認無 code 讀 axiom env**

Run:
```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
grep -rn "AXIOM_API_KEY\|AXIOM_DATASET\|axiom_api_key\|axiom_dataset" backend_py/src/ backend_py/tests/ | grep -v __pycache__
grep -rn "AXIOM" .github/ backend_py/.env backend_py/.env.example 2>/dev/null
```
Expected（src/tests）：只剩 config.py。

- [ ] **Step 2: 刪 config.py axiom 欄位**

`config.py`：刪 `axiom_api_key`/`axiom_dataset` dataclass 欄位、`load_config` 內 `os.environ[...AXIOM...]` 讀取與 `raise ValueError("AXIOM_API_KEY required")` 驗證、建構 kwargs 的這兩項。

- [ ] **Step 3: 改 CI**

`.github/workflows/ci.yml`：刪 `AXIOM_API_KEY: ${{ secrets... }}` / `AXIOM_DATASET:` 兩行；:109 註解「Axiom round-trip」→「PG event_log read-your-writes」；integration step 保留（仍需 `DATABASE_URL`/`BFX_DEPLOYMENT_ENV`）。

- [ ] **Step 4: .env**

`grep` 若 `backend_py/.env`（symlink 至 repo root .env）有 `AXIOM_*` 行則刪。`.env.example` 確認無。

- [ ] **Step 5: 驗證**

Run:
```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
# config 載入 smoke（確認移除 axiom env 後仍可 load）
BFX_PHASE=paper BFX_DEPLOYMENT_ENV=ci uv run python -c "from bfx_funding_bot.modules.marketfeed.config import load_config; load_config(); print('config ok')" || echo "（需其他必要 env，視 load_config 而定）"
```
Expected: PASS + clean

- [ ] **Step 6: Commit**
```bash
git add -A
git commit -m "🔧 Chore: remove AXIOM_API_KEY/AXIOM_DATASET from config + CI + .env (3c T14)"
```

---

## Phase F — 收尾：殘留註解 + spec

### Task 15: 清殘留 Axiom 用語 + spec §13.1 記錄 3c shipped

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/bus.py`（:7-8/:57 註解 Axiom→PG event_log）
- Modify: `src/bfx_funding_bot/modules/execution/events.py`、`event_upcasters.py`（docstring「Axiom row」→「event row / PG event_log」）
- Modify: 其餘 grep 出的 Axiom 註解/字串殘留
- Modify: `docs/superpowers/specs/2026-05-23-postgres-event-store-sot-migration-design.md`（§13.1 加 item 11：3c shipped）

- [ ] **Step 1: 全 repo grep 殘留**

Run: `cd /Users/will/second-brain/projects/startup/bfx-funding-bot && grep -rin "axiom" backend_py/src/ backend_py/tests/ | grep -v __pycache__`
Expected: 僅剩註解/docstring（無 code identifier）。逐一清理或確認是合理的歷史脈絡引用。

- [ ] **Step 2: 改註解/docstring** 於 bus.py/events.py/event_upcasters.py 等，Axiom 用語改 PG event_log。

- [ ] **Step 3: spec 記錄 3c shipped**

`spec §13.1` 末加 item 11，仿 item 10 格式：列 commit 範圍、落地內容（StdoutEventSink、PostgresEventLogQueryAdapter、deployment_env 解耦、G1/paper 退役）、驗證數字（unit/integration count、mypy/ruff/alembic）、確認 item 10 全部 follow-ups resolved。更新 §410 的「3a 後修訂的 Plan 3 順序」標 3c done。`§8`/`§9` 可加註「3c 完成」。

- [ ] **Step 4: 最終全量驗證**

Run:
```bash
cd backend_py
uv run pytest -m "not integration" -q 2>&1 | tail -3
uv run mypy src/ && uv run ruff check
uv run alembic check    # 確認無新 drift（baseline drift 是既有技術債，非 3c 引入）
git grep -in "axiom" -- backend_py/src backend_py/tests | grep -v __pycache__ || echo "no axiom identifiers left"
```
Expected: unit 綠（數字 ≈ 基準 −40）、mypy/ruff clean、無 axiom code identifier 殘留。

- [ ] **Step 5: Commit**
```bash
git add -A
git commit -m "📝 Docs: clean residual Axiom mentions + record 3c shipped in spec §13.1 (3c T15)"
```

---

## Self-Review（plan 對 spec 覆蓋檢查）

對照 spec §8 / §13.1 item 1,3,9,10：
- §8 刪 AxiomClient/Config → **T10** ✅；replay adapter → **T9** ✅；boot axiom init+replay → **T7/T10** ✅；axiom_sink→diagnostics（3b 已做）+ 移除冗餘 SoT emit → **T8** ✅；移除 AXIOM env → **T14** ✅；改寫 T12 整合測試 → **T3（新 PG read-your-writes）+ T12（刪舊）** ✅。
- item 1（牽動 7 module）：signal_engine/health_monitor/fill_tracker → **T4**；smoke_runner/L3 → **T6**；g1.py → **T13** ✅。
- item 3（L3 → PG read-your-writes）→ **T3+T6** ✅。
- item 9（明確不在 3b → 3c）：刪 client/config/adapter **T9/T10/T11**；HEALTH_CHECK/SIGNAL/lifecycle → stdout **T4**；L3 → PG **T6**；移除 axiom_sink 冗餘 SoT emit **T8**；env/CI **T14**；deployment_environment 解耦 **T2/T7** ✅。
- item 10 follow-ups：`handle_cancel_*` dead 隨 sink 刪 **T8**；`_AxiomProtocol`/`_CaptureAxiom` rename **T4/T5**（operational→`_EventSink`、diagnostics→`_DiagnosticsProtocol`、fake→`_EventCapture`）；`kind=safety_trigger` payload 異質 —— 評估後**不在 3c 強制正規化**（3b 已在 `daemon_smoke_boot.py` defensive query，異質 payload 仍 lossless 存 diagnostics，正規化屬 observability 平台引入時的獨立決策，§358 反 gold-plating）；`deployment_environment` 解耦 **T2/T7** ✅。
- **非 3c**（item 10 末）：`alembic check` baseline drift 屬獨立技術債，T15 只驗證「3c 未新增 drift」，不修 baseline。

潛在風險 / 執行時注意：
- 行號全為 2026-05-24 盤點值，**以 grep anchor 重新定位**。
- `EventResource` 與 `StdoutEventSink` 的 envelope 合併方向以 `AxiomClient` 既有行為為準（T1 Step 1 確認）。
- daemon `session_factory` 變數名 grep 確認（T6 smoke wiring）。
- `test_daemon_axiom_wiring.py` 跨 T4/T10：T4 先讓它過（改斷言或暫留），T10 決定重寫或刪。
- L3 smoke 改 PG 後，須確認 L3 流程的事件真的經 executor→EventStorePersister 落 event_log（T6 Step 1 驗證），否則 poll 不到。
