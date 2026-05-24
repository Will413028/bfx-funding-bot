# PG Event-Store 3a-write (A2 Write-Ahead Intent) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 SoT 事件持久化從 Plan 2 的 async `PostgresEventSink`（bus 訂閱、跨系統無 txn）收進 command txn 內，落實 A2 write-ahead intent —— 送單前持久化 `RESERVATION_INTENT`、送單後持久化 outcome，每筆 outcome 都是 event_log + snapshot 同 txn。

**Architecture:** `ReservationEmittingMiddleware` 變成同步持久化點：先在 middleware 中央化算出 `cid`（capture-once submit_date），txn1 寫 INTENT（PENDING）並 commit，呼叫 `inner.submit(cid)`，txn2 寫 outcome（CLAIMED/FILL 或 FAILED）並 commit，最後才 `bus.publish` 給記憶體投影（ledger/registry）+ axiom（待 3c）。每次持久化各自一個 txn，結構性保證「never 跨 REST call 持 txn」。`offer_claims` 投影改 cid-keyed 狀態機；voi-keyed `transition()` 留給記憶體 `OfferRegistry`。Plan 2 的 `PostgresEventSink` 退役刪除。

**Tech Stack:** Python 3.13、SQLAlchemy 2.0 async、Alembic、pytest（unit + testcontainers integration）、PostgreSQL（Neon）。所有指令在 `backend_py/` 下用 `uv run` 執行。

**鎖定設計來源：** `docs/superpowers/specs/2026-05-23-postgres-event-store-sot-migration-design.md` §5（write path）、§13.4（同步 in-command txn）、§13.2（composite PK）、§13.6（cid thread submit_date）。

---

## File Structure

新增：
- `src/bfx_funding_bot/modules/execution/event_store/persister.py` — `EventStorePersister`（包 store + session_factory，`persist(*events)` 開一個 txn append N 筆）+ `NoopEventPersister`（null object，給不測持久化的 chain test 用）+ `EventPersister` Protocol。職責：把「在一個 txn 內 append 事件」封裝起來，使 middleware 的 txn1/txn2 邊界結構性成立。
- `alembic/versions/c7d1e2f3a4b5_offer_claims_composite_pk.py` — `offer_claims` PK 從 `(cid)` 改 `(account_id, deployment_environment, cid)`，並 drop 冗餘 `idx_offer_claims_acct_env`。revises `4f8c2e91b3a7`。
- `tests/integration/test_reservation_write_path.py` — A2 write-path integration tests（testcontainers PG）。

改寫：
- `src/bfx_funding_bot/modules/execution/events.py` — 新增 `ReservationIntent` / `ReservationFailed` frozen dataclass。
- `src/bfx_funding_bot/modules/marketfeed/schemas.py` — `EventType` 加 `RESERVATION_INTENT` / `RESERVATION_FAILED`。
- `src/bfx_funding_bot/modules/execution/event_store/serialization.py` — `_TYPE_BY_CLASS` 註冊兩枚新事件。
- `src/bfx_funding_bot/modules/execution/event_store/tables.py` — `OfferClaimRow` composite PK。
- `src/bfx_funding_bot/modules/execution/registry_offers.py` — `RegistryState` 加 `FAILED`。
- `src/bfx_funding_bot/modules/execution/event_store/store.py` — `_project_offer_claims` 改 cid-keyed 狀態機 + `_upsert_claim` composite on_conflict + 移除已不再使用的 `transition`/`ClaimRecord`/`_row_to_claim`。
- `src/bfx_funding_bot/modules/execution/protocols.py` — `ExecutorPort.submit` 加 `cid` keyword 參數。
- `src/bfx_funding_bot/modules/execution/paper.py` / `src/bfx_funding_bot/external/bitfinex/live_executor.py` — `submit` 吃傳入 cid（None 時 fallback 自產，保留直接呼叫 ergonomics）。
- `src/bfx_funding_bot/modules/execution/registry.py` — `ExecutorSpec` 加 `is_simulated` 欄位；`TransientRetryMiddleware` 轉發 cid。
- `src/bfx_funding_bot/modules/execution/middleware/transient_retry.py` — `submit` 加 cid 並轉發。
- `src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py` — 重寫為 A2 同步持久化。
- `src/bfx_funding_bot/modules/marketfeed/daemon.py` — 移除 pg_sink 訂閱與 import、改 middleware 建構（傳 persister + is_simulated）、更新 from_snapshot 註解。

刪除（退役 Plan 2 過渡件）：
- `src/bfx_funding_bot/modules/execution/event_store/sink.py`
- `tests/modules/execution/event_store/test_sink.py`

測試更新（建構簽章 ripple）：
- `tests/modules/execution/middleware/test_reservation_emitting.py` — 重寫（新建構子 + 注入 recording persister）。
- `tests/integration/test_daemon_pg_cutover.py` — `PostgresEventSink` → `EventStorePersister`。
- `tests/modules/admin/test_smoke_runner.py`、`tests/modules/admin/test_integration_full_chain.py`、`tests/modules/admin/test_integration_http.py`、`tests/modules/marketfeed/test_daemon_phase43_executor_chain.py` — 建構 `ReservationEmittingMiddleware` 處補 `persister=NoopEventPersister()`。

**核心設計決策（spec 已鎖定，實作時遵守）：**

1. **cid 走 optional keyword 貫穿 chain**：`ExecutorPort.submit(decision, ctx, *, cid: int | None = None)`。Heartbeat（在 ReservationEmitting 外層）以 `(decision, ctx)` 呼叫 → ReservationEmitting 自算 cid（忽略傳入值）→ 往下傳 `cid=<算出>` → TransientRetry 轉發 → executor 用傳入 cid。executor 在 `cid is None` 時 fallback 自產（保留 test_paper.py 等直接呼叫不需改）。production 永遠由 middleware 傳入，INTENT/outcome cid 一致由「middleware 算一次傳下去」保證。

2. **持久化收進 EventStorePersister**：每次 `persister.persist(*events)` 開一個 `session_scope`（一個 txn）。middleware 在 submit 前 `persist(intent)`（txn1），submit 後 `persist(claimed[, filled])` 或 `persist(failed)`（txn2）。各自獨立 txn → 結構性「never 跨 REST 持 txn」。

3. **INTENT/FAILED 只進 PG，不上 bus**：記憶體 ledger（reserved/realized）+ registry（voi-keyed）不訂閱 INTENT/FAILED（PENDING 無 voi、FAILED reserved 不動）。bus 只 publish CLAIMED + FILL（同現況）。

4. **offer_claims cid-keyed**：`_project_offer_claims` 改成直接 `event_type → state` 映射（INTENT→PENDING/voi=NULL、CLAIMED→CLAIMED/+voi、FAILED→FAILED、ORDER_FILL→RELEASED、RELEASED→RELEASED），不再走 voi-keyed `transition()`，不 pre-select（無 in-session stale 風險，移除舊 identity-map expire）。

---

## Task 1: ReservationIntent / ReservationFailed domain events + EventType

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/events.py`
- Modify: `src/bfx_funding_bot/modules/marketfeed/schemas.py:35-48`
- Test: `tests/modules/execution/test_events.py`

- [ ] **Step 1: 寫 failing test（events 新欄位）**

加到 `tests/modules/execution/test_events.py` 檔尾（沿用該檔既有 import 風格；若檔內尚無 `Decimal`/`UUID`/`ReservationIntent`/`ReservationFailed` import 則補上）：

```python
from decimal import Decimal
from uuid import UUID

from bfx_funding_bot.modules.execution.events import (
    ReservationFailed,
    ReservationIntent,
)

_SCID_T1 = UUID("11111111-1111-1111-1111-111111111111")


def test_reservation_intent_fields() -> None:
    ev = ReservationIntent(
        cid=42,
        size_usdt=Decimal("100"),
        signal_correlation_id=_SCID_T1,
        account_id="acct",
        is_simulated=True,
        occurred_at_ms=1000,
    )
    assert ev.cid == 42
    assert ev.size_usdt == Decimal("100")
    assert ev.account_id == "acct"
    # uniform bitemporal optionals default None
    assert ev.event_seq is None
    assert ev.recorded_at_ms is None
    # INTENT carries no venue_offer_id (PENDING — voi unknown until CLAIMED)
    assert not hasattr(ev, "venue_offer_id")


def test_reservation_failed_fields() -> None:
    ev = ReservationFailed(
        cid=42,
        size_usdt=Decimal("100"),
        signal_correlation_id=_SCID_T1,
        account_id="acct",
        is_simulated=False,
        reason="submit_failed",
        occurred_at_ms=2000,
    )
    assert ev.reason == "submit_failed"
    assert ev.is_simulated is False
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_events.py -q`
Expected: FAIL — `ImportError: cannot import name 'ReservationIntent'`

- [ ] **Step 3: 加兩枚 domain event**

在 `src/bfx_funding_bot/modules/execution/events.py` 的 `ReservationClaimed` 定義之前（`__SCHEMA_VERSION__ = 2` 之後）插入：

```python
@dataclass(frozen=True, slots=True)
class ReservationIntent:
    """A2 write-ahead intent — durable record BEFORE the venue REST submit.

    Persisted in txn1 so a crash between submit-call and outcome leaves a
    recoverable PENDING claim (resolved at boot in 3a-recovery). Carries no
    venue_offer_id (unknown until CLAIMED). Not published to the bus
    (in-memory ledger/registry track CLAIMED+, not PENDING).
    """
    cid: int
    size_usdt: Decimal
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReservationFailed:
    """A2 terminal outcome — venue REST submit failed; intent resolves to FAILED.

    Ledger effect: none (reserved untouched — capital was never committed).
    Not published to the bus (no in-memory subscriber needs it).
    """
    cid: int
    size_usdt: Decimal
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    reason: str  # e.g. "submit_failed"
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
```

- [ ] **Step 4: 加 EventType 兩枚**

在 `src/bfx_funding_bot/modules/marketfeed/schemas.py` 的 `EventType` enum，`RESERVATION_CLAIMED` 那行之前插入兩行（保持與既有 `reservation_*` 命名一致）：

```python
    RESERVATION_INTENT = "reservation_intent"
    RESERVATION_CLAIMED = "reservation_claimed"
    RESERVATION_FAILED = "reservation_failed"
    RESERVATION_RELEASED = "reservation_released"
```

（即在現有 `RESERVATION_CLAIMED` / `RESERVATION_RELEASED` 之間與之前補上 INTENT、FAILED 兩枚；不要刪除既有成員。）

- [ ] **Step 5: 跑測試確認 pass + mypy**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_events.py -q && uv run mypy src/bfx_funding_bot/modules/execution/events.py`
Expected: PASS；mypy no issues

- [ ] **Step 6: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/events.py \
        src/bfx_funding_bot/modules/marketfeed/schemas.py \
        tests/modules/execution/test_events.py
git commit -m "✨ Feat: add ReservationIntent / ReservationFailed domain events (A2 write-ahead)"
```

---

## Task 2: serialization 註冊兩枚新事件

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/event_store/serialization.py:8-19`
- Test: `tests/modules/execution/event_store/test_serialization.py`

- [ ] **Step 1: 寫 failing test（round-trip + event_type_of）**

加到 `tests/modules/execution/event_store/test_serialization.py`：

```python
from bfx_funding_bot.modules.execution.events import (
    ReservationFailed,
    ReservationIntent,
)


def test_intent_failed_event_type_of() -> None:
    intent = ReservationIntent(cid=1, size_usdt=Decimal("5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000)
    failed = ReservationFailed(cid=1, size_usdt=Decimal("5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        reason="submit_failed", occurred_at_ms=1000)
    assert event_type_of(intent) == "RESERVATION_INTENT"
    assert event_type_of(failed) == "RESERVATION_FAILED"


@pytest.mark.parametrize("event", [
    ReservationIntent(cid=9, size_usdt=Decimal("7.5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000),
    ReservationFailed(cid=9, size_usdt=Decimal("7.5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=False,
        reason="submit_failed", occurred_at_ms=2000),
])
def test_intent_failed_roundtrip(event: object) -> None:
    etype = event_type_of(event)
    restored = deserialize_event(etype, serialize_event(event))
    assert restored == event
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_serialization.py -q`
Expected: FAIL — `ValueError: unserializable event type: ReservationIntent`

- [ ] **Step 3: 註冊到 `_TYPE_BY_CLASS`**

在 `src/bfx_funding_bot/modules/execution/event_store/serialization.py` 改 import + mapping：

```python
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationReleased,
)

# event_type string <-> domain class. Clean field names (we own this schema).
_TYPE_BY_CLASS: dict[type, str] = {
    ReservationIntent: "RESERVATION_INTENT",
    ReservationClaimed: "RESERVATION_CLAIMED",
    ReservationFailed: "RESERVATION_FAILED",
    OrderFilled: "ORDER_FILL",
    ReservationReleased: "RESERVATION_RELEASED",
}
```

（`_CLASS_BY_TYPE`、`_FIELDS`、`_DECIMAL_FIELDS`、`_UUID_FIELDS` 不動 —— 它們由 `_TYPE_BY_CLASS` 推導，`size_usdt`/`signal_correlation_id` 在新事件同名同型，自動涵蓋。）

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_serialization.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/event_store/serialization.py \
        tests/modules/execution/event_store/test_serialization.py
git commit -m "✨ Feat: register ReservationIntent / ReservationFailed in event serialization"
```

---

## Task 3: offer_claims composite PK（migration + tables.py + RegistryState.FAILED）

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/registry_offers.py:55-59`
- Modify: `src/bfx_funding_bot/modules/execution/event_store/tables.py:65-84`
- Create: `alembic/versions/c7d1e2f3a4b5_offer_claims_composite_pk.py`
- Test: `tests/modules/execution/event_store/test_tables_metadata.py`

- [ ] **Step 1: 寫 failing test（RegistryState.FAILED + composite PK metadata）**

加到 `tests/modules/execution/event_store/test_tables_metadata.py`（沿用該檔既有 import；補上需要的）：

```python
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.registry_offers import RegistryState


def test_registry_state_has_failed() -> None:
    assert RegistryState("failed") is RegistryState.FAILED


def test_offer_claims_composite_pk() -> None:
    pk_cols = [c.name for c in OfferClaimRow.__table__.primary_key.columns]
    assert pk_cols == ["account_id", "deployment_environment", "cid"]
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_tables_metadata.py -q`
Expected: FAIL — `ValueError: 'failed' is not a valid RegistryState`（及 PK assertion）

- [ ] **Step 3: 加 RegistryState.FAILED**

`src/bfx_funding_bot/modules/execution/registry_offers.py`：

```python
class RegistryState(Enum):
    PENDING = "pending"   # A2 write-ahead intent — durable, voi unknown
    CLAIMED = "claimed"
    RELEASED = "released"
    FAILED = "failed"     # A2 submit-failed terminal
```

- [ ] **Step 4: 改 OfferClaimRow composite PK**

`src/bfx_funding_bot/modules/execution/event_store/tables.py` 的 `OfferClaimRow`：cid 拿掉 `primary_key=True`，改用 `PrimaryKeyConstraint`，並移除冗餘 `idx_offer_claims_acct_env`（composite PK 前綴已涵蓋 `(account_id, deployment_environment)` 查詢）：

```python
class OfferClaimRow(Base):
    """Snapshot: offer FSM projection, cid-keyed (composite PK with tenant scope)."""

    __tablename__ = "offer_claims"

    cid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    signal_correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_updated_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("account_id", "deployment_environment", "cid"),
        Index("idx_offer_claims_voi", "venue_offer_id"),
    )
```

（`PrimaryKeyConstraint` 已 import；`Index` 已 import。）

- [ ] **Step 5: 寫 migration**

新建 `alembic/versions/c7d1e2f3a4b5_offer_claims_composite_pk.py`。PK 變更 alembic autogenerate 不可靠，手寫。pre-launch 空庫；即使有 shadow 資料，舊 PK 是單欄 `cid`（全域唯一）→ composite 必然唯一 → 變更安全。

```python
"""offer_claims composite PK (account_id, deployment_environment, cid)

Revision ID: c7d1e2f3a4b5
Revises: 4f8c2e91b3a7
Create Date: 2026-05-24 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c7d1e2f3a4b5"
down_revision: str | Sequence[str] | None = "4f8c2e91b3a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the now-redundant index (composite PK prefix covers acct+env lookups).
    op.drop_index("idx_offer_claims_acct_env", table_name="offer_claims")
    # Swap single-col PK (cid) -> composite (account_id, deployment_environment, cid).
    op.drop_constraint("offer_claims_pkey", "offer_claims", type_="primary")
    op.create_primary_key(
        "offer_claims_pkey",
        "offer_claims",
        ["account_id", "deployment_environment", "cid"],
    )


def downgrade() -> None:
    op.drop_constraint("offer_claims_pkey", "offer_claims", type_="primary")
    op.create_primary_key("offer_claims_pkey", "offer_claims", ["cid"])
    op.create_index(
        "idx_offer_claims_acct_env",
        "offer_claims",
        ["account_id", "deployment_environment"],
    )
```

- [ ] **Step 6: 跑 metadata test + alembic check**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_tables_metadata.py -q`
Expected: PASS

Run: `cd backend_py && uv run alembic upgrade head && uv run alembic check`
Expected: migration 套用成功；`alembic check` 回報 metadata 與 DB 無 drift（tables.py 的 composite PK 與 migration 結果一致）

> 若 `alembic check` 仍偵測到 `idx_offer_claims_acct_env` 差異，代表 metadata 與 migration 不一致 —— 確認 Step 4 已移除該 Index 定義。

- [ ] **Step 7: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/registry_offers.py \
        src/bfx_funding_bot/modules/execution/event_store/tables.py \
        alembic/versions/c7d1e2f3a4b5_offer_claims_composite_pk.py \
        tests/modules/execution/event_store/test_tables_metadata.py
git commit -m "✨ Feat: offer_claims composite PK (account, env, cid) + RegistryState.FAILED"
```

---

## Task 4: store.py `_project_offer_claims` cid-keyed 狀態機 + composite on_conflict

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/event_store/store.py`
- Test: `tests/modules/execution/event_store/test_store_append_unit.py`

- [ ] **Step 1: 寫 failing test（cid-keyed projection over INTENT→CLAIMED→FILL/FAILED）**

加到 `tests/modules/execution/event_store/test_store_append_unit.py`（沿用該檔的 `sqlite_session` fixture + `_create_all` helper；補 import）：

```python
from bfx_funding_bot.modules.execution.events import (
    ReservationFailed,
    ReservationIntent,
)


async def test_intent_creates_pending_claim_with_null_voi(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=300, size_usdt=Decimal("8"), signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, occurred_at_ms=1000))
    await sqlite_session.flush()
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 300))).scalar_one()
    assert row.state == "pending"
    assert row.venue_offer_id is None
    assert row.size_usdt == Decimal("8")


async def test_intent_then_claimed_updates_same_cid_row(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=301, size_usdt=Decimal("8"), signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, occurred_at_ms=1000))
    await store.append(sqlite_session, ReservationClaimed(
        cid=301, venue_offer_id="v301", size_usdt=Decimal("8"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=1, occurred_at_ms=1100))
    await sqlite_session.flush()
    rows = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 301))).scalars().all()
    assert len(rows) == 1  # cid-keyed: PENDING row promoted in place, not a 2nd row
    assert rows[0].state == "claimed"
    assert rows[0].venue_offer_id == "v301"


async def test_intent_then_failed_marks_failed_reserved_untouched(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=302, size_usdt=Decimal("8"), signal_correlation_id=_SCID,
        account_id="acctF", is_simulated=True, occurred_at_ms=1000))
    await store.append(sqlite_session, ReservationFailed(
        cid=302, size_usdt=Decimal("8"), signal_correlation_id=_SCID,
        account_id="acctF", is_simulated=True, reason="submit_failed",
        occurred_at_ms=1100))
    await sqlite_session.flush()
    claim = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 302))).scalar_one()
    assert claim.state == "failed"
    ps = (await sqlite_session.execute(
        select(PositionStateRow).where(PositionStateRow.account_id == "acctF"))).scalar_one()
    assert ps.reserved_usdt == Decimal("0")   # FAILED never reserved capital
    assert ps.last_event_seq > 0              # high-water mark still advances
```

注意：`test_intent_then_failed_...` 需 import `PositionStateRow`（該檔已 import `EventLogRow, OfferClaimRow` —— 補 `PositionStateRow`）。

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_store_append_unit.py -q`
Expected: FAIL — INTENT 目前因 voi=None 被 `_project_offer_claims` 早退（`if venue_offer_id is None: return`），無 PENDING row → `NoResultFound`

- [ ] **Step 3: 改 store.py imports + 加 state map**

`src/bfx_funding_bot/modules/execution/event_store/store.py` 頂部：把 `from ...registry_offers import ClaimRecord, RegistryState, transition` 改成只留 `RegistryState`：

```python
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
```

在 `_DEDUP_TYPES` 定義之後新增：

```python
# offer_claims FSM state by event_type — cid-keyed projection. The voi-keyed
# transition() (registry_offers.py) is reserved for the in-memory OfferRegistry's
# fill-tracking; this snapshot is keyed by cid (stable across the whole lifecycle).
_CLAIM_STATE_BY_TYPE: dict[str, RegistryState] = {
    "RESERVATION_INTENT": RegistryState.PENDING,
    "RESERVATION_CLAIMED": RegistryState.CLAIMED,
    "RESERVATION_FAILED": RegistryState.FAILED,
    "ORDER_FILL": RegistryState.RELEASED,
    "RESERVATION_RELEASED": RegistryState.RELEASED,
}
```

- [ ] **Step 4: 改 `append` 對 `_project_offer_claims` 的呼叫**

`append` 內把：

```python
        await self._project_offer_claims(session, event, account_id, venue_offer_id)
```

改為（移除 venue_offer_id 參數，由 projection 內部取）：

```python
        await self._project_offer_claims(session, event, account_id)
```

- [ ] **Step 5: 重寫 `_project_offer_claims` + `_upsert_claim`，刪 `_row_to_claim`**

把現有 `_project_offer_claims`、`_upsert_claim`、檔尾 `_row_to_claim` 整段換成：

```python
    async def _project_offer_claims(
        self,
        session: AsyncSession,
        event: object,
        account_id: str,
    ) -> None:
        """Project event onto the cid-keyed offer_claims snapshot (same txn).

        Direct event_type -> state mapping; no pre-select, no voi-keyed
        transition(). cid is stable across the whole lifecycle so a single
        row is upserted in place (PENDING -> CLAIMED -> RELEASED/FAILED).
        """
        etype = event_type_of(event)
        state = _CLAIM_STATE_BY_TYPE.get(etype)
        if state is None:
            return  # audit-only events (e.g. cancel) are not claim-bearing
        _ev: Any = cast(Any, event)
        cid: int | None = getattr(_ev, "cid", None)
        if cid is None:
            return  # no cid -> nothing to key on
        now_ms: int = _ev.occurred_at_ms or 0
        await self._upsert_claim(
            session,
            cid=cid,
            account_id=account_id,
            state=state,
            venue_offer_id=getattr(_ev, "venue_offer_id", None),
            size_usdt=Decimal(str(_ev.size_usdt)),
            signal_correlation_id=str(_ev.signal_correlation_id),
            occurred_at_ms=now_ms,
            last_updated_ms=now_ms,
        )

    async def _upsert_claim(
        self,
        session: AsyncSession,
        *,
        cid: int,
        account_id: str,
        state: RegistryState,
        venue_offer_id: str | None,
        size_usdt: Decimal,
        signal_correlation_id: str,
        occurred_at_ms: int,
        last_updated_ms: int,
    ) -> None:
        dialect = session.bind.dialect.name if session.bind else "postgresql"
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        values: dict[str, Any] = {
            "cid": cid,
            "account_id": account_id,
            "deployment_environment": self._env,
            "state": state.value,
            "venue_offer_id": venue_offer_id,
            "size_usdt": size_usdt,
            "signal_correlation_id": signal_correlation_id,
            "occurred_at_ms": occurred_at_ms,
            "last_updated_ms": last_updated_ms,
            # FSM state is the SoT for claims; position_state carries the
            # high-water mark, so last_event_seq stays 0 here (by-design,
            # carry-forward (d)).
            "last_event_seq": 0,
        }
        stmt = ins(OfferClaimRow).values(values).on_conflict_do_update(
            index_elements=["account_id", "deployment_environment", "cid"],
            set_={k: values[k] for k in ("state", "venue_offer_id", "last_updated_ms")},
        )
        await session.execute(stmt)
```

說明：(a) on_conflict index_elements 改 composite，對齊 Task 3 的 composite PK；(b) 移除舊的 identity-map expire —— 新 projection 不再 pre-select OfferClaimRow，同 session 內無 stale 快取風險；(c) `_row_to_claim`、`ClaimRecord`、`transition` 在 store 內已無使用者，整段刪除。

- [ ] **Step 6: 改 `rebuild_snapshot_from_log` 對 `_project_offer_claims` 的呼叫**

把：

```python
            await self._project_offer_claims(session, event, account_id, r.venue_offer_id)
```

改為：

```python
            await self._project_offer_claims(session, event, account_id)
```

- [ ] **Step 7: 跑 store unit tests + 既有 integration 投影 test + mypy**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_store_append_unit.py -q && uv run mypy src/bfx_funding_bot/modules/execution/event_store/store.py`
Expected: PASS；mypy clean

> 既有 `test_store_append_unit.py::test_claim_then_release_updates_offer_claims` 仍應 PASS：CLAIMED→RELEASED 在 cid-keyed 下同樣產出單一 cid row（claimed→released）。

- [ ] **Step 8: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/event_store/store.py \
        tests/modules/execution/event_store/test_store_append_unit.py
git commit -m "♻️ Refactor: offer_claims projection cid-keyed + composite on_conflict (A2)"
```

---

## Task 5: ExecutorPort.submit 加 cid 參數 + paper/live 改吃 cid + ExecutorSpec.is_simulated

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/protocols.py:62-66`
- Modify: `src/bfx_funding_bot/modules/execution/paper.py:58-88`
- Modify: `src/bfx_funding_bot/external/bitfinex/live_executor.py:183-216`
- Modify: `src/bfx_funding_bot/modules/execution/middleware/transient_retry.py:25-28`
- Modify: `src/bfx_funding_bot/modules/execution/registry.py:37-41,79-85,104-111`
- Test: `tests/modules/execution/test_paper.py`

- [ ] **Step 1: 寫 failing test（傳入 cid 被採用；不傳則 fallback 自產）**

加到 `tests/modules/execution/test_paper.py`：

```python
@pytest.mark.asyncio
async def test_submit_uses_provided_cid() -> None:
    axiom = _CaptureAxiom()
    ex = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        date_provider=lambda: date(2026, 5, 21),
    )
    result = await ex.submit(_decision(uuid4()), _ctx(), cid=99999)
    assert result.cid == 99999
    # emitted order_submit/order_fill carry the injected cid
    assert axiom.events[0]["payload"]["cid"] == 99999


@pytest.mark.asyncio
async def test_submit_without_cid_falls_back_to_generated() -> None:
    axiom = _CaptureAxiom()
    ex = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        date_provider=lambda: date(2026, 5, 21),
    )
    corr = uuid4()
    r1 = await ex.submit(_decision(corr), _ctx())          # no cid
    r2 = await ex.submit(_decision(corr), _ctx(), cid=None) # explicit None
    assert r1.cid == r2.cid  # both fall back to deterministic generate_cid
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_paper.py -q`
Expected: FAIL — `TypeError: submit() got an unexpected keyword argument 'cid'`

- [ ] **Step 3: ExecutorPort protocol 加 cid**

`src/bfx_funding_bot/modules/execution/protocols.py`：

```python
class ExecutorPort(Protocol):
    """Venue executor (Echo paper / Bitfinex live).

    cid is centralized by ReservationEmittingMiddleware (A2: same cid for INTENT
    + outcome). It is threaded down through the chain; executors use it when
    provided and fall back to deterministic generation only for direct callers.
    """
    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
    ) -> SubmittedOrder: ...
```

- [ ] **Step 4: paper.py 吃傳入 cid**

`src/bfx_funding_bot/modules/execution/paper.py` 的 `submit`：

```python
    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
    ) -> SubmittedOrder:
        # cid is centralized by ReservationEmittingMiddleware (A2). Direct callers
        # (tests) omit it -> fall back to deterministic generation. CC2: capture
        # date once at submit entry (midnight-race immune).
        if cid is None:
            submit_date = self._date_provider()
            cid = generate_cid(decision.signal_correlation_id, submit_date)
        # CC4: "paper_" prefix is invariant relied on by fill_tracker to skip
        # venue polling for simulated offers.
        offer_id = f"paper_{uuid.uuid4().hex[:12]}"
        ...
```

（其餘 body 不動：`emit_order_submit` / `emit_order_fill` / `return SubmittedOrder(cid=cid, ...)` 照舊用 `cid`。）

- [ ] **Step 5: live_executor.py 吃傳入 cid**

`src/bfx_funding_bot/external/bitfinex/live_executor.py` 的 `submit`：

```python
    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
    ) -> SubmittedOrder:
        # cid centralized by ReservationEmittingMiddleware (A2); direct callers
        # fall back to deterministic generation (CC2 capture-once date).
        if cid is None:
            cid = generate_cid(decision.signal_correlation_id, self._date_provider())
        payload = build_offer_payload(
            symbol="fUSD",
            ...
            cid=cid,
        )
        ...
```

（其餘不動。）

- [ ] **Step 6: TransientRetryMiddleware 轉發 cid**

`src/bfx_funding_bot/modules/execution/middleware/transient_retry.py`：

```python
    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
    ) -> SubmittedOrder:
        return await self._submit_retried(decision, ctx, cid=cid)
```

（`transient_retry` 的 wrapper 是 `*args, **kwargs` 直通，cid kwarg 會被原樣轉發給 `inner.submit` —— 已確認 `retry.py:61` 簽章。）

- [ ] **Step 7: ExecutorSpec 加 is_simulated + 兩處 return 填值**

`src/bfx_funding_bot/modules/execution/registry.py`：

```python
@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    executor: ExecutorPort
    fill_tracker_enabled: bool
    ws_client_enabled: bool
    is_simulated: bool  # paper -> True; bitfinex_live -> False (drives A2 event payload)
```

paper return（line ~79）加 `is_simulated=True`：

```python
        return ExecutorSpec(
            executor=EchoPaperExecutor(
                axiom=axiom, phase=phase, strategy=strategy, cell=cell,
            ),
            fill_tracker_enabled=False,
            ws_client_enabled=False,
            is_simulated=True,
        )
```

live return（line ~104）加 `is_simulated=False`：

```python
        return ExecutorSpec(
            executor=BitfinexLiveExecutor(
                http=http, axiom=axiom, bus=bus,
                phase=phase, strategy=strategy, cell=cell,
            ),
            fill_tracker_enabled=fill_tracker_enabled,
            ws_client_enabled=ws_client_enabled,
            is_simulated=False,
        )
```

- [ ] **Step 8: 跑 paper test + executor-chain 相關 test + mypy**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_paper.py tests/modules/marketfeed/test_daemon_phase43_executor_chain.py -q -m "not integration"`
Expected: PASS（既有 test_paper.py 三個 cid 測試靠 fallback 不需改；新增兩個 PASS）

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/execution/protocols.py src/bfx_funding_bot/modules/execution/paper.py src/bfx_funding_bot/modules/execution/registry.py src/bfx_funding_bot/modules/execution/middleware/transient_retry.py src/bfx_funding_bot/external/bitfinex/live_executor.py`
Expected: mypy clean

- [ ] **Step 9: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/protocols.py \
        src/bfx_funding_bot/modules/execution/paper.py \
        src/bfx_funding_bot/external/bitfinex/live_executor.py \
        src/bfx_funding_bot/modules/execution/middleware/transient_retry.py \
        src/bfx_funding_bot/modules/execution/registry.py \
        tests/modules/execution/test_paper.py
git commit -m "✨ Feat: centralize cid via ExecutorPort.submit(cid=) + ExecutorSpec.is_simulated"
```

---

## Task 6: EventStorePersister + middleware A2 同步持久化 + daemon 退役 pg_sink

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/event_store/persister.py`
- Modify: `src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py`
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py:59,683-688,806-826`
- Delete: `src/bfx_funding_bot/modules/execution/event_store/sink.py`
- Delete: `tests/modules/execution/event_store/test_sink.py`
- Modify (test ripple): `tests/modules/execution/middleware/test_reservation_emitting.py`（重寫）、`tests/integration/test_daemon_pg_cutover.py`、`tests/modules/admin/test_smoke_runner.py`、`tests/modules/admin/test_integration_full_chain.py`、`tests/modules/admin/test_integration_http.py`、`tests/modules/marketfeed/test_daemon_phase43_executor_chain.py`

- [ ] **Step 1: 寫 EventStorePersister（含 failing-import test 在重寫的 middleware test 內）**

新建 `src/bfx_funding_bot/modules/execution/event_store/persister.py`：

```python
"""Synchronous event persistence for the A2 write path.

Each persist() call opens ONE txn (session_scope) and appends N events via
PostgresEventStore. The middleware calls persist() before the venue submit
(txn1: INTENT) and after it returns (txn2: outcome) — separate calls => separate
txns => structurally "never hold a txn across a REST call" (spec §13.4).
"""
from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore


class EventPersister(Protocol):
    async def persist(self, *events: object) -> None: ...


class EventStorePersister:
    """Real persister: append events in a single committed txn."""

    def __init__(
        self,
        *,
        store: PostgresEventStore,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._store = store
        self._session_factory = session_factory

    async def persist(self, *events: object) -> None:
        async with session_scope(self._session_factory) as session:
            for event in events:
                await self._store.append(session, event)


class NoopEventPersister:
    """Null object — for chain tests / contexts that don't exercise persistence."""

    async def persist(self, *events: object) -> None:
        return None
```

- [ ] **Step 2: 重寫 middleware test（failing first）**

整檔重寫 `tests/modules/execution/middleware/test_reservation_emitting.py`：

```python
"""ReservationEmittingMiddleware — A2 write-ahead intent + sync persistence."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import ExecutorTransientError
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
)
from bfx_funding_bot.modules.execution.middleware.reservation_emitting import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    )


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


class _RecordingPersister:
    """Records each persist() call as one tuple of events (= one txn)."""
    def __init__(self) -> None:
        self.txns: list[tuple[object, ...]] = []

    async def persist(self, *events: object) -> None:
        self.txns.append(events)


class _StubInner:
    """Echoes the injected cid back in the SubmittedOrder (A2 contract)."""
    def __init__(self, status: str, voi: str | None, *, persister: _RecordingPersister | None = None) -> None:
        self._status = status
        self._voi = voi
        self._persister = persister
        self.persist_calls_at_submit: int | None = None
        self.cid_seen: int | None = None

    async def submit(self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None) -> SubmittedOrder:
        self.cid_seen = cid
        if self._persister is not None:
            self.persist_calls_at_submit = len(self._persister.txns)
        return SubmittedOrder(cid=cid or 0, venue_offer_id=self._voi, status=self._status, raw_response=None)


def _bus_capture() -> tuple[DomainEventBus, list[object]]:
    bus = DomainEventBus()
    seen: list[object] = []
    async def _on(e: object) -> None:
        seen.append(e)
    bus.subscribe(ReservationClaimed, _on)
    bus.subscribe(OrderFilled, _on)
    return bus, seen


@pytest.mark.asyncio
async def test_paper_filled_persists_intent_then_claim_and_fill() -> None:
    bus, seen = _bus_capture()
    persister = _RecordingPersister()
    inner = _StubInner("filled", "paper_abc", persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=bus, persister=persister, is_simulated=True)
    await mw.submit(_decision(), _ctx())
    # txn1 = INTENT (alone); txn2 = CLAIMED + FILL
    assert len(persister.txns) == 2
    assert [type(e) for e in persister.txns[0]] == [ReservationIntent]
    assert [type(e) for e in persister.txns[1]] == [ReservationClaimed, OrderFilled]
    # INTENT landed BEFORE submit
    assert inner.persist_calls_at_submit == 1
    # bus only gets CLAIMED + FILL (never INTENT)
    assert [type(e) for e in seen] == [ReservationClaimed, OrderFilled]


@pytest.mark.asyncio
async def test_live_submitted_persists_intent_then_claim_only() -> None:
    bus, seen = _bus_capture()
    persister = _RecordingPersister()
    inner = _StubInner("submitted", "123456", persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=bus, persister=persister, is_simulated=False)
    await mw.submit(_decision(), _ctx())
    assert [type(e) for e in persister.txns[0]] == [ReservationIntent]
    assert [type(e) for e in persister.txns[1]] == [ReservationClaimed]
    assert [type(e) for e in seen] == [ReservationClaimed]
    assert persister.txns[1][0].is_simulated is False


@pytest.mark.asyncio
async def test_failed_persists_intent_then_failed_no_publish() -> None:
    bus, seen = _bus_capture()
    persister = _RecordingPersister()
    inner = _StubInner("failed", None, persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=bus, persister=persister, is_simulated=False)
    await mw.submit(_decision(), _ctx())
    assert [type(e) for e in persister.txns[0]] == [ReservationIntent]
    assert [type(e) for e in persister.txns[1]] == [ReservationFailed]
    assert seen == []  # FAILED never published to bus


@pytest.mark.asyncio
async def test_same_cid_threaded_to_inner_and_all_events() -> None:
    bus, _ = _bus_capture()
    persister = _RecordingPersister()
    inner = _StubInner("filled", "paper_x", persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=bus, persister=persister, is_simulated=True)
    await mw.submit(_decision(), _ctx())
    intent_cid = persister.txns[0][0].cid
    claim_cid = persister.txns[1][0].cid
    assert inner.cid_seen == intent_cid == claim_cid  # one cid everywhere


@pytest.mark.asyncio
async def test_bus_publish_failure_does_not_break_submit() -> None:
    class _BrokenBus:
        async def publish(self, event: object) -> None:
            raise RuntimeError("bus publish broken")
    persister = _RecordingPersister()
    inner = _StubInner("filled", "paper_abc", persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=_BrokenBus(), persister=persister, is_simulated=True)  # type: ignore[arg-type]
    result = await mw.submit(_decision(), _ctx())
    assert result.status == "filled"  # persistence already committed; publish best-effort


@pytest.mark.asyncio
async def test_inner_raise_after_intent_propagates() -> None:
    """Crash between INTENT (txn1, committed) and outcome -> exception propagates;
    INTENT is already durable (verified in integration tests)."""
    class _RaisingInner:
        async def submit(self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None) -> SubmittedOrder:
            raise ExecutorTransientError("blip")
    bus, _ = _bus_capture()
    persister = _RecordingPersister()
    mw = ReservationEmittingMiddleware(_RaisingInner(), bus=bus, persister=persister, is_simulated=True)
    with pytest.raises(ExecutorTransientError):
        await mw.submit(_decision(), _ctx())
    assert [type(e) for e in persister.txns[0]] == [ReservationIntent]  # txn1 ran
    assert len(persister.txns) == 1  # no outcome txn
```

- [ ] **Step 3: 跑測試確認 fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/middleware/test_reservation_emitting.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'persister'`

- [ ] **Step 4: 重寫 ReservationEmittingMiddleware**

整檔重寫 `src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py`：

```python
"""ReservationEmittingMiddleware — A2 write-ahead intent + sync SoT persistence.

Flow per submit:
  1. compute cid once (capture-once submit_date — INTENT/outcome share it)
  2. txn1: persist ReservationIntent (PENDING) — durable BEFORE venue submit
  3. inner.submit(cid) — the only non-transactional boundary (Bitfinex REST)
  4. txn2: persist outcome (CLAIMED[+ORDER_FILL] | RESERVATION_FAILED)
  5. bus.publish CLAIMED[+FILL] for in-memory projections (ledger/registry) +
     diagnostics. INTENT/FAILED are NOT published (no in-memory subscriber).

Each persist() is its own txn (EventStorePersister) so a txn is never held
across the REST call. A crash between txn1 and txn2 leaves a durable PENDING
claim, resolved at boot (3a-recovery).

I3-EM: status="failed" -> persist FAILED, no bus publish.
I4-EM: bus.publish raise -> swallow + log (persistence already committed).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import EventPersister
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload

log = logging.getLogger(__name__)


class ReservationEmittingMiddleware:
    """ExecutorPort wrapper: A2 write-ahead intent + sync persistence + bus fanout."""

    def __init__(
        self,
        inner: ExecutorPort,
        *,
        bus: DomainEventBus,
        persister: EventPersister,
        is_simulated: bool = True,
        clock: Callable[[], int] | None = None,
        date_provider: Callable[[], date] | None = None,
    ) -> None:
        self._inner = inner
        self._bus = bus
        self._persister = persister
        self._is_simulated = is_simulated
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._date_provider = date_provider or (lambda: datetime.now(UTC).date())

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
    ) -> SubmittedOrder:
        # This middleware is the cid authority (A2). Ignore any incoming cid;
        # compute once so INTENT and outcome share the exact same value.
        cid = generate_cid(decision.signal_correlation_id, self._date_provider())
        size = Decimal(str(decision.offer_amount_usdt or 0.0))
        scid = decision.signal_correlation_id
        intent_ms = self._clock()

        # txn1: write-ahead intent (durable before the venue submit)
        await self._persister.persist(ReservationIntent(
            cid=cid, size_usdt=size, signal_correlation_id=scid,
            account_id=ctx.account_id, is_simulated=self._is_simulated,
            occurred_at_ms=intent_ms,
        ))

        result = await self._inner.submit(decision, ctx, cid=cid)
        outcome_ms = self._clock()

        if result.status in ("submitted", "filled"):
            claimed = ReservationClaimed(
                cid=cid, venue_offer_id=result.venue_offer_id or "",
                size_usdt=size, signal_correlation_id=scid,
                account_id=ctx.account_id, is_simulated=self._is_simulated,
                occurred_at_ms=outcome_ms,
            )
            filled: OrderFilled | None = None
            if result.status == "filled":
                filled = OrderFilled(
                    cid=cid, venue_offer_id=result.venue_offer_id or "", credit_id=None,
                    size_usdt=size, fill_rate=decision.offer_rate or 0.0,
                    signal_correlation_id=scid, account_id=ctx.account_id,
                    is_simulated=self._is_simulated, occurred_at_ms=outcome_ms,
                )
            # txn2: outcome (event_log + snapshot, atomic)
            if filled is not None:
                await self._persister.persist(claimed, filled)
            else:
                await self._persister.persist(claimed)
            # in-memory projections + diagnostics (after durable commit)
            await self._safe_publish(claimed)
            if filled is not None:
                await self._safe_publish(filled)
        elif result.status == "failed":
            # txn2: FAILED — reserved untouched
            await self._persister.persist(ReservationFailed(
                cid=cid, size_usdt=size, signal_correlation_id=scid,
                account_id=ctx.account_id, is_simulated=self._is_simulated,
                reason="submit_failed", occurred_at_ms=outcome_ms,
            ))
        return result

    async def _safe_publish(self, event: object) -> None:
        """I4-EM: bus failure must not fail submit. Persistence is already committed;
        bus is best-effort in-memory fanout."""
        try:
            await self._bus.publish(event)
        except Exception as exc:
            log.critical(
                "bus_publish_failed_outer event=%s err=%r — projection lost, "
                "SoT already persisted",
                type(event).__name__, exc,
            )
```

- [ ] **Step 5: 跑 middleware test 確認 pass + mypy**

Run: `cd backend_py && uv run pytest tests/modules/execution/middleware/test_reservation_emitting.py -q && uv run mypy src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py src/bfx_funding_bot/modules/execution/event_store/persister.py`
Expected: PASS；mypy clean

- [ ] **Step 6: daemon 退役 pg_sink + 改 middleware 建構**

`src/bfx_funding_bot/modules/marketfeed/daemon.py`：

(a) line 59 把 `from ...event_store.sink import PostgresEventSink` 換成：

```python
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
```

(b) line 683-686 的 from_snapshot 註解，把「written by PostgresEventSink at runtime」改為：

```python
    # Phase 4.4c / 3a: PG event-store replaces Axiom replay at boot.
    # from_snapshot reads position_state + offer_claims from Postgres (written
    # synchronously by ReservationEmittingMiddleware in the command txn — A2).
```

(c) 刪除 line 806-809（pg_sink 建構 + 3 個 subscribe）：

```python
    pg_sink = PostgresEventSink(store=event_store, session_factory=session_factory)
    bus.subscribe(ReservationClaimed,  pg_sink.on_reservation_claimed)
    bus.subscribe(OrderFilled,         pg_sink.on_order_filled)
    bus.subscribe(ReservationReleased, pg_sink.on_reservation_released)
```

(d) line 820-826 改 middleware 建構（注入 persister + is_simulated）：

```python
    persister = EventStorePersister(store=event_store, session_factory=session_factory)
    wrapped_executor = HeartbeatMiddleware(
        ReservationEmittingMiddleware(
            TransientRetryMiddleware(executor),
            bus=bus,
            persister=persister,
            is_simulated=spec.is_simulated,
        ),
        probe=probe,
    )
```

（`event_store` 已在 line 688 建好仍保留；`ledger`/`axiom_sink`/`offer_registry` 的 subscribe 不動 —— axiom_sink 待 3c 移除。）

- [ ] **Step 7: 刪除 sink.py + test_sink.py，改 cutover test 用 persister**

```bash
git rm src/bfx_funding_bot/modules/execution/event_store/sink.py \
       tests/modules/execution/event_store/test_sink.py
```

`tests/integration/test_daemon_pg_cutover.py`：把 import 與用法從 `PostgresEventSink` 換成 `EventStorePersister`：

- import 行 `from ...event_store.sink import PostgresEventSink` → `from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister`
- `sink = PostgresEventSink(store=store, session_factory=pg_session_factory)` → `persister = EventStorePersister(store=store, session_factory=pg_session_factory)`
- `await sink.on_reservation_claimed(ReservationClaimed(...))` → `await persister.persist(ReservationClaimed(...))`
- 任何 `await sink.on_order_filled(...)` / `on_reservation_released(...)` 同樣改 `await persister.persist(...)`

（語意等價：persister.persist 與舊 sink 都是「一個 txn 內 store.append 一筆」。）

- [ ] **Step 8: chain-test 建構補 persister=NoopEventPersister()**

以下 4 檔每個建構 `ReservationEmittingMiddleware(paper, bus=bus)` 的點，補成 `ReservationEmittingMiddleware(paper, bus=bus, persister=NoopEventPersister())`，並在各檔 import：

```python
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
```

- `tests/modules/admin/test_smoke_runner.py`（line ~61，1 處）
- `tests/modules/admin/test_integration_full_chain.py`（line ~70, ~105，2 處）
- `tests/modules/admin/test_integration_http.py`（line ~73, ~119，2 處）
- `tests/modules/marketfeed/test_daemon_phase43_executor_chain.py`（line ~96, ~184，2 處）

> 這些測試的目的是 chain wiring / smoke / http，不驗持久化，故用 null object。

- [ ] **Step 9: 跑受影響 unit + 全 unit suite + mypy**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_smoke_runner.py tests/modules/admin/test_integration_full_chain.py tests/modules/admin/test_integration_http.py tests/modules/marketfeed/test_daemon_phase43_executor_chain.py -q -m "not integration"`
Expected: PASS

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: PASS（全 unit suite 綠 —— 確認沒有殘留 `PostgresEventSink` import）

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: mypy clean、ruff clean（特別檢查 daemon.py 無未使用 import）

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "♻️ Refactor: A2 sync persistence in middleware; retire async PostgresEventSink"
```

---

## Task 7: Integration tests（A2 write path，testcontainers PG）

**Files:**
- Create: `tests/integration/test_reservation_write_path.py`

- [ ] **Step 1: 寫 integration tests（INTENT-before-submit / 同 cid 更新 / FAILED / crash-PENDING）**

新建 `tests/integration/test_reservation_write_path.py`：

```python
"""A2 write-path integration tests — real Postgres (testcontainers).

Run: cd backend_py && uv run pytest tests/integration/test_reservation_write_path.py -q -m integration
"""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.middleware.reservation_emitting import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload

pytestmark = pytest.mark.integration
_ENV = "ci"


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    )


def _ctx(account_id: str) -> AccountContext:
    return AccountContext(
        account_id=account_id,
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


def _mw(inner, pg_session_factory, *, account_simulated: bool = True) -> ReservationEmittingMiddleware:
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    return ReservationEmittingMiddleware(
        inner, bus=DomainEventBus(), persister=persister, is_simulated=account_simulated,
    )


class _PgAssertingInner:
    """On submit, asserts (read-your-writes) the INTENT is already a committed
    PENDING row before returning the chosen outcome."""
    def __init__(self, *, pg_session_factory, account_id: str, status: str, voi: str | None) -> None:
        self._sf = pg_session_factory
        self._account_id = account_id
        self._status = status
        self._voi = voi

    async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
        async with self._sf() as s:
            row = (await s.execute(select(OfferClaimRow).where(
                OfferClaimRow.cid == cid,
                OfferClaimRow.account_id == self._account_id,
                OfferClaimRow.deployment_environment == _ENV,
            ))).scalar_one()
            assert row.state == "pending"
            assert row.venue_offer_id is None
        return SubmittedOrder(cid=cid or 0, venue_offer_id=self._voi, status=self._status, raw_response=None)


async def test_intent_committed_before_submit(pg_session_factory) -> None:
    acct = "wp_intent"
    inner = _PgAssertingInner(pg_session_factory=pg_session_factory, account_id=acct,
                              status="submitted", voi="v_intent")
    mw = _mw(inner, pg_session_factory)
    await mw.submit(_decision(), _ctx(acct))  # inner asserts PENDING visible mid-flight


async def test_claimed_updates_same_cid_row(pg_session_factory) -> None:
    acct = "wp_claim"

    class _Inner:
        async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
            return SubmittedOrder(cid=cid or 0, venue_offer_id="v_claim", status="submitted", raw_response=None)

    mw = _mw(_Inner(), pg_session_factory, account_simulated=False)
    await mw.submit(_decision(), _ctx(acct))
    async with pg_session_factory() as s:
        rows = (await s.execute(select(OfferClaimRow).where(
            OfferClaimRow.account_id == acct,
            OfferClaimRow.deployment_environment == _ENV,
        ))).scalars().all()
        assert len(rows) == 1                       # PENDING promoted in place
        assert rows[0].state == "claimed"
        assert rows[0].venue_offer_id == "v_claim"
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == acct,
            PositionStateRow.deployment_environment == _ENV,
        ))).scalar_one()
        assert ps.reserved_usdt == Decimal("100")   # CLAIMED reserved += size


async def test_failed_marks_failed_reserved_zero(pg_session_factory) -> None:
    acct = "wp_failed"

    class _Inner:
        async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
            return SubmittedOrder(cid=cid or 0, venue_offer_id=None, status="failed", raw_response=None)

    mw = _mw(_Inner(), pg_session_factory, account_simulated=False)
    await mw.submit(_decision(), _ctx(acct))
    async with pg_session_factory() as s:
        claim = (await s.execute(select(OfferClaimRow).where(
            OfferClaimRow.account_id == acct,
            OfferClaimRow.deployment_environment == _ENV,
        ))).scalar_one()
        assert claim.state == "failed"
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == acct,
            PositionStateRow.deployment_environment == _ENV,
        ))).scalar_one()
        assert ps.reserved_usdt == Decimal("0")     # FAILED never reserves


async def test_crash_mid_flight_leaves_pending(pg_session_factory) -> None:
    acct = "wp_crash"

    class _RaisingInner:
        async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
            raise RuntimeError("crash between INTENT and outcome")

    mw = _mw(_RaisingInner(), pg_session_factory)
    with pytest.raises(RuntimeError):
        await mw.submit(_decision(), _ctx(acct))
    async with pg_session_factory() as s:
        rows = (await s.execute(select(OfferClaimRow).where(
            OfferClaimRow.account_id == acct,
            OfferClaimRow.deployment_environment == _ENV,
        ))).scalars().all()
        assert len(rows) == 1
        assert rows[0].state == "pending"           # durable PENDING for 3a-recovery
        assert rows[0].venue_offer_id is None
        # event_log has exactly the INTENT row, no outcome
        cnt = (await s.execute(select(func.count()).select_from(OfferClaimRow).where(
            OfferClaimRow.account_id == acct))).scalar_one()
        assert cnt == 1
```

- [ ] **Step 2: 跑 integration tests（需 Docker / testcontainers）**

Run: `cd backend_py && uv run pytest tests/integration/test_reservation_write_path.py -q -m integration`
Expected: 4 passed

> 若環境無 Docker，testcontainers 會 skip/error；在有 Docker 的環境執行。

- [ ] **Step 3: 跑既有 PG integration（迴歸：cutover + event store 投影）**

Run: `cd backend_py && uv run pytest tests/integration/test_pg_event_store.py tests/integration/test_daemon_pg_cutover.py -q -m integration`
Expected: PASS（composite PK + cid-keyed projection 對既有 voi-carrying 事件向後相容）

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_reservation_write_path.py
git commit -m "✅ Test: A2 write-path integration (intent-before-submit, same-cid, failed, crash-PENDING)"
```

---

## Final Verification

- [ ] **全 unit suite**：`cd backend_py && uv run pytest -m "not integration" -q` → 全綠
- [ ] **型別 + lint**：`cd backend_py && uv run mypy src/ && uv run ruff check` → clean
- [ ] **migration drift**：`cd backend_py && uv run alembic check` → no drift
- [ ] **PG integration**（有 Docker 時）：`cd backend_py && uv run pytest -m integration -q` → 全綠
- [ ] **殘留掃描**：`grep -rn "PostgresEventSink" src/ tests/` → 無結果（已完全退役）

---

## Self-Review（plan 對照 spec §13.4 / §5 / §13.2 / §13.6）

**Spec coverage：**
- §13.4「event store append 在 command txn 內 + write-ahead intent」→ Task 6 middleware txn1/txn2 + EventStorePersister。✓
- §13.4「bus 降為純 in-memory projection + diagnostics fanout」→ Task 6 移除 pg_sink、保留 ledger/registry/axiom subscribe、INTENT/FAILED 不上 bus。✓
- §13.4「offer_claims 投影改 cid-keyed」→ Task 4。✓
- §13.2「offer_claims PK 改 (account_id, deployment_environment, cid)」→ Task 3 migration + tables.py。✓
- §5 write path（INTENT 送單前、CLAIMED/FILL/FAILED 送單後同 txn）→ Task 6 submit 流程。✓
- §13.6「cid 必須在送單前可算 + thread submit_date」→ Task 5（ExecutorPort.submit cid param）+ Task 6（middleware capture-once `generate_cid`）。✓
- §13.1「3a-write 只做 A2 寫入；boot 仍走現有 from_snapshot，PENDING rows 被 voi-not-null filter 排除無 regression」→ `OfferRegistry.from_snapshot` 既有 `venue_offer_id.is_not(None)` filter（registry_offers.py:283）保證 PENDING/FAILED 不入記憶體 registry；本 plan 不動 boot，符合。✓
- 明確 out-of-scope（留 3a-recovery）：signed `get_active_funding_offers()`、boot resolve PENDING、venue reconcile —— 本 plan 不碰。✓

**Placeholder scan：** 無 TBD / "add error handling" / 無程式碼的步驟；每個 code step 附完整程式碼與確切指令。✓

**Type consistency：**
- `cid: int`（events / SubmittedOrder / OfferClaimRow / generate_cid 回傳）全程一致。✓
- `_project_offer_claims(session, event, account_id)` 三參數簽章在 `append` 與 `rebuild_snapshot_from_log` 兩呼叫處一致（Task 4 Step 4 + Step 6）。✓
- `_upsert_claim` keyword-only 新簽章與唯一呼叫者 `_project_offer_claims` 一致。✓
- `EventPersister.persist(*events)` 在 `EventStorePersister`、`NoopEventPersister`、middleware、測試 `_RecordingPersister` 四處簽章一致。✓
- `ReservationEmittingMiddleware.__init__(inner, *, bus, persister, is_simulated, clock, date_provider)` 在 daemon、4 個 chain test、middleware test、integration test 建構處一致。✓
- `ExecutorPort.submit(decision, ctx, *, cid=None)` 在 protocol / paper / live / TransientRetry / 所有 stub inner 一致；Heartbeat 與 ReservationEmitting 上層以 `(decision, ctx)` 呼叫（cid optional 預設 None）相容。✓
- `RegistryState.FAILED` 加入後，`store._CLAIM_STATE_BY_TYPE` 用 `RegistryState.FAILED`、`from_snapshot` 的 `RegistryState(r.state)` 能解析 "failed" 但被 voi filter 排除。✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-24-pg-event-store-a2-write-path.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每個 task 派 fresh subagent、task 間兩段式 review、快速迭代。

**2. Inline Execution** — 本 session 內用 executing-plans 批次執行、checkpoint review。

依原始指示走 subagent-driven-development。注意 subagent 跑 pytest/mypy/alembic 一律先 `cd backend_py` 走 uv 管的 Python 3.13（見 repo CLAUDE.md）。
