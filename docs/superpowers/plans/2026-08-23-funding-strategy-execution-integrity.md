# Funding Strategy Execution Integrity & Rate Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 fail-closed、period-correct、可稽核的 Bitfinex 放貸執行邊界，並在不破壞 signal floor 的前提下，以可驗證的 empirical fill evidence 支援 rate optimizer。

**Architecture:** `DeploymentReconciler` 只產生 candidate；`ExecutionGate` 以 exact-period `MarketSnapshot`、高信心 `FillModelEvidence` 與 safety result 判斷 `ReadyToSubmit`、`BlockedExecution` 或 `NoRecommendation`。`ReadyToSubmit` 必須先寫入 append-only `execution_decisions` 並 commit，才可交給 executor；executor 的型別介面不再接受 raw `DecisionPayload`。Bitfinex funding book 由 public WebSocket 維持，REST 只做 snapshot/reconciliation；ticker 不再參與執行定價。

**Tech Stack:** Python 3.13、asyncio、Pydantic、SQLAlchemy 2.0 async、Alembic、httpx、websockets、pytest、mypy、ruff、Prometheus client。

**Spec:** `docs/superpowers/specs/2026-08-23-funding-strategy-optimization-design.md`

## Global Constraints

- 只修改 repository；不執行 remote command、container restart、deploy、cap change、rate change 或 resume call。
- 所有後端指令從 `backend_py/` 執行，使用 `uv run` 管理 Python 3.13。
- 每個 production code change 先寫對應 unit/contract test；每個 task 結尾執行該 task 的 targeted tests。
- 缺少、過期、symbol/period 不匹配、sequence/checksum 無效、depth 不足、model missing 或 model low-confidence 一律產生明確 blocked/incomplete result；不得重用原 quote、scalar ticker 或 linear estimate。
- executor 的唯一 submit 入口是 `submit(ready: ReadyToSubmit, ...)`；不得新增接收 raw `DecisionPayload` 的 overload 或 fallback adapter。
- `execution_decisions` 是 pre-trade audit，不進 ledger projection；READY audit commit 失敗時不得發 venue request。
- Prometheus label 只使用 bounded enum：`outcome`、`reason`、`policy`、`result`；symbol、cell、model hash 與 raw exception 只進結構化 log/audit。
- `/healthz` 只表示 process liveness；market/model blocked 只影響 `trading_ready`、`/readyz` 與 operator state，不讓 liveness probe 觸發 restart loop。
- `cells.canary.yaml`、`safety.canary.yaml` 不修改；p14 僅能以 `BFX_PHASE=shadow`、`BFX_DEPLOYMENT_ENV=shadow` 啟動。
- migration 一律使用 `cd backend_py && uv run alembic upgrade head`；不得用 MCP 或手寫 SQL 直接套用 production schema。
- 工作樹中既有 staged deletions 不屬於本計畫，實作與 commit 不得 stage、還原或修改：`.agents/skills/neon-postgres/SKILL.md`、`.claire/worktrees/ws-foc-parser-fix/backend_py/tests/external/bitfinex/fixtures/foc_canceled.json`。

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `backend_py/src/bfx_funding_bot/modules/execution/contracts.py` | execution policy、stable block reason、typed outcomes、`ReadyToSubmit` boundary values |
| `backend_py/src/bfx_funding_bot/external/bitfinex/funding_book_ws.py` | Bitfinex funding-book WS subscribe、snapshot/update/heartbeat/sequence/checksum parser |
| `backend_py/src/bfx_funding_bot/modules/marketfeed/funding_book.py` | immutable `MarketSnapshot`、in-memory book、REST resync 與 freshness/depth provider |
| `backend_py/src/bfx_funding_bot/modules/execution/audit/__init__.py` | audit module export surface |
| `backend_py/src/bfx_funding_bot/modules/execution/audit/tables.py` | SQLAlchemy `ExecutionDecisionRow` append-only table |
| `backend_py/src/bfx_funding_bot/modules/execution/audit/model.py` | immutable `ExecutionDecision` record and serialization |
| `backend_py/src/bfx_funding_bot/modules/execution/audit/recorder.py` | transaction-owning `ExecutionDecisionRecorder` and typed persistence failure |
| `backend_py/src/bfx_funding_bot/modules/execution/deployment/period_pricing.py` | exact-period maker/taker price decision; no fallback branch |
| `backend_py/src/bfx_funding_bot/modules/execution/deployment/eligibility.py` | policy-aware gate, audit-before-submit ordering, readiness updates |
| `backend_py/src/bfx_funding_bot/modules/execution/deployment/rate_optimizer.py` | deterministic candidate generation/scoring for optimizer shadow/live |
| `backend_py/src/bfx_funding_bot/modules/marketfeed/readiness.py` | bounded trading-readiness state and block reason snapshot |
| `backend_py/src/bfx_funding_bot/modules/lending/tracking/artifact.py` | fill-model artifact metadata and unavailable result |
| `backend_py/alembic/versions/f4a7c9d1e2b3_add_execution_decisions.py` | audit table migration from current head `d437f9d4e3fe` |
| `backend_py/alembic/versions/f5b8d0e2f3c4_add_fill_model_artifacts.py` | model artifact metadata migration |
| `deploy/vm/shadow-p14.env` | locked AdaptivePeriod p14 shadow profile |

### Modified files

| File | Responsibility after this plan |
|---|---|
| `backend_py/src/bfx_funding_bot/modules/execution/protocols.py` | executor/guard ports consume typed contracts |
| `backend_py/src/bfx_funding_bot/modules/marketfeed/config.py` | explicit `ExecutionPolicy` and book/model dependency validation |
| `backend_py/src/bfx_funding_bot/modules/marketfeed/schemas.py` | stable execution event payloads and audit correlation id |
| `backend_py/src/bfx_funding_bot/modules/execution/events.py` | `ReservationIntent.execution_decision_id` link |
| `backend_py/src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py` | write-ahead intent carries decision id and unwraps `ReadyToSubmit` |
| `backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py` | accepts only `ReadyToSubmit` |
| `backend_py/src/bfx_funding_bot/modules/execution/paper.py` | accepts only `ReadyToSubmit` |
| `backend_py/src/bfx_funding_bot/modules/observability/tracing.py` | propagates typed submit value through middleware |
| `backend_py/src/bfx_funding_bot/modules/observability/metrics.py` | bounded execution/book/readiness metrics |
| `backend_py/src/bfx_funding_bot/modules/observability/stdout_sink.py` | fixed event names and bounded structured attributes |
| `backend_py/src/bfx_funding_bot/modules/marketfeed/healthz.py` | separate `/healthz` liveness and `/readyz` trading readiness |
| `backend_py/src/bfx_funding_bot/modules/admin/trading_status.py` | exposes readiness reason without changing liveness semantics |
| `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py` | removes ticker/clamp fallback and routes every candidate through gate |
| `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` | wires book service, audit recorder, gate, readiness and lifecycle |
| `backend_py/src/bfx_funding_bot/modules/lending/tracking/tables.py` | fill-model artifact metadata table |
| `backend_py/src/bfx_funding_bot/modules/lending/tracking/repository.py` | loads only versioned artifact-compatible stats |
| `backend_py/src/bfx_funding_bot/modules/lending/tracking/model.py` | returns evidence or typed unavailable result |
| `backend_py/src/bfx_funding_bot/modules/lending/tracking/fill_rate.py` | learner emits artifact scope metadata |
| `backend_py/src/bfx_funding_bot/modules/backtest/config.py` | explicit `empirical`/`linear-baseline` mode, no implicit default in callers |
| `backend_py/src/bfx_funding_bot/modules/backtest/engine.py` | empirical missing/low-confidence becomes incomplete |
| `backend_py/src/bfx_funding_bot/modules/backtest/schemas.py` | incomplete status and evidence metadata |
| `backend_py/src/bfx_funding_bot/modules/backtest/matrix.py` | model/config propagation and incomplete-window aggregation |
| `backend_py/src/bfx_funding_bot/modules/backtest/oos_eval.py` | required config/model and explicit baseline labeling |
| `backend_py/src/bfx_funding_bot/modules/backtest/cell_derivation.py` | required model/config for cell derivation |
| `backend_py/src/bfx_funding_bot/modules/backtest/cell_pipeline.py` | passes model/config into derivation |
| `backend_py/scripts/learn_fill_rate.py` | writes versioned artifact metadata |
| `backend_py/scripts/run_phase3b_wfo_matrix.py` | empirical model loading and `linear-baseline` explicit flag |
| `backend_py/scripts/run_oos_profitability.py` | explicit config/model selection and report labels |
| `backend_py/scripts/derive_cells.py` | explicit model/config loading |
| `scripts/deploy-vm.sh` | supports `shadow-p14` without changing canary guard |
| `deploy/vm/paper.env`, `deploy/vm/shadow.env`, `deploy/vm/canary.env` | declare `BFX_EXECUTION_POLICY` and remove `BFX_CLAMP_*` |
| `.env.example`, `backend_py/.env.example` | document required policy/book/model settings |
| `backend_py/alembic/env.py` | imports audit/artifact tables into metadata |
| `backend_py/ARCHITECTURE.md` | records the new execution/audit/readiness source of truth |

### Deleted files

| File | Reason |
|---|---|
| `backend_py/src/bfx_funding_bot/modules/execution/deployment/book_clamp.py` | scalar ticker clamp and `FALLBACK` branch are replaced by exact-period pricing |
| `backend_py/tests/modules/execution/deployment/test_book_clamp.py` | replaced by period-pricing fail-closed contract tests |

---

## Task 1: Freeze execution-domain contracts and explicit policy configuration

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/execution/contracts.py`
- Create: `backend_py/tests/modules/execution/test_contracts.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/protocols.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/config.py`
- Modify: `backend_py/tests/modules/marketfeed/test_config.py`
- Modify: `.env.example`, `backend_py/.env.example`

**Interfaces:**
- Produces `ExecutionPolicy(StrEnum)` with exactly `PAPER`, `BOOK_GUARDED`, `OPTIMIZER_SHADOW`, `OPTIMIZER_LIVE` values `paper`, `book_guarded`, `optimizer_shadow`, `optimizer_live`.
- Produces `DecisionOutcome(StrEnum)` with exactly `READY`, `BLOCKED`, `NO_RECOMMENDATION`.
- Produces `BlockReason(StrEnum)` with the twelve stable values in the spec: `book_fetch_failed`, `book_not_initialized`, `book_stale`, `book_sequence_invalid`, `book_checksum_invalid`, `period_not_found`, `insufficient_period_depth`, `fill_model_missing`, `fill_model_low_confidence`, `optimizer_unavailable`, `safety_guard_blocked`, `execution_audit_unavailable`.
- Produces frozen `ReadyToSubmit(decision: DecisionPayload, decision_id: str, policy: ExecutionPolicy, market_snapshot_id: str, model_version: str | None, evidence: Mapping[str, object], safety: GuardResult)`.
- Produces frozen `BlockedExecution(decision_id: str, candidate: DecisionPayload, reason: BlockReason, failed_dependency: str, evidence: Mapping[str, object])` and `NoRecommendation(decision_id: str, candidate: DecisionPayload | None, reason: BlockReason, evidence: Mapping[str, object])`.
- Changes `ExecutorPort.submit` to `submit(self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None) -> SubmittedOrder`.
- Adds `MarketfeedConfig.execution_policy`, `book_max_age_seconds`, `book_reconcile_interval_seconds`, `book_max_down_pct`, and `optimizer_fee_rate`; live-capable policies require explicit non-empty values for their dependencies.

- [ ] **Step 1: Write failing contract and config tests**

~~~python
def test_missing_execution_policy_fails_closed(monkeypatch):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_EXECUTION_POLICY", raising=False)

    with pytest.raises(ValueError, match="BFX_EXECUTION_POLICY"):
        load_config()


def test_canary_rejects_paper_policy(monkeypatch):
    _set_required_config_env(monkeypatch, phase="canary", policy="paper")

    with pytest.raises(ValueError, match="canary.*execution_policy"):
        load_config()


def test_blocked_execution_is_not_submit_ready():
    blocked = BlockedExecution(
        decision_id="d-1",
        candidate=_decision(),
        reason=BlockReason.BOOK_STALE,
        failed_dependency="market_snapshot",
        evidence={"age_ms": 31_000},
    )
    assert blocked.outcome is DecisionOutcome.BLOCKED
    assert not isinstance(blocked, ReadyToSubmit)
~~~

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_contracts.py tests/modules/marketfeed/test_config.py -q`

Expected: FAIL because the typed outcomes and `BFX_EXECUTION_POLICY` parser do not exist.

- [ ] **Step 3: Implement the contracts and config validation**

Use frozen dataclasses with `slots=True`; expose `outcome` as a property returning the corresponding `DecisionOutcome`. Move `GuardResult` into `contracts.py` and import it from `protocols.py` so `ReadyToSubmit` has one canonical safety-result type. Parse `BFX_EXECUTION_POLICY` with `ExecutionPolicy(value)` and reject missing/unknown values. Require `BFX_BOOK_MAX_AGE_SECONDS`, `BFX_BOOK_RECONCILE_INTERVAL_SECONDS`, and `BFX_BOOK_MAX_DOWN_PCT` for `book_guarded`, `optimizer_shadow`, and `optimizer_live`; require `BFX_FILL_MODEL_ARTIFACT` for `optimizer_live`. Reject `BFX_CLAMP_ENABLED`, `BFX_CLAMP_MAX_DOWN_PCT`, and `BFX_CLAMP_TAKER_MAX_PERIOD_D` instead of reading them.

- [ ] **Step 4: Run the focused tests and type check**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_contracts.py tests/modules/marketfeed/test_config.py -q && uv run mypy src/bfx_funding_bot/modules/execution/contracts.py src/bfx_funding_bot/modules/execution/protocols.py src/bfx_funding_bot/modules/marketfeed/config.py`

Expected: PASS; all callers in the focused config test file set an explicit policy.

- [ ] **Step 5: Commit the contract boundary**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution/contracts.py backend_py/src/bfx_funding_bot/modules/execution/protocols.py backend_py/src/bfx_funding_bot/modules/marketfeed/config.py backend_py/tests/modules/execution/test_contracts.py backend_py/tests/modules/marketfeed/test_config.py .env.example backend_py/.env.example
git commit -m "✨ Feat: add explicit execution policy contracts"
~~~

## Task 2: Build the period-correct funding-book snapshot service

**Files:**
- Create: `backend_py/src/bfx_funding_bot/external/bitfinex/funding_book_ws.py`
- Create: `backend_py/src/bfx_funding_bot/modules/marketfeed/funding_book.py`
- Create: `backend_py/tests/external/bitfinex/test_funding_book_ws.py`
- Create: `backend_py/tests/modules/marketfeed/test_funding_book.py`
- Modify: `backend_py/src/bfx_funding_bot/external/bitfinex/rest.py`

**Interfaces:**
- Produces frozen `MarketSnapshot(snapshot_id: str, symbol: str, bids: tuple[FundingBookLevel, ...], asks: tuple[FundingBookLevel, ...], captured_at_ms: int, received_at_ms: int, source: Literal["ws", "rest_reconciled"], sequence_valid: bool, checksum_valid: bool, sequence: int | None)`.
- Produces `FundingBookProvider.snapshot(symbol: str, *, now_ms: int) -> MarketSnapshot | None`.
- Produces `FundingBookService.start()`, `FundingBookService.stop()`, `FundingBookService.run(stop_event: asyncio.Event)`, and `FundingBookService.snapshot(...)`.
- `FundingBookLevel` continues to normalize Bitfinex `[RATE, PERIOD, COUNT, AMOUNT]`; `amount > 0` is ask and `amount < 0` is bid. Use `Decimal(str(...))` at the policy boundary.
- WS parser subscribes with `channel=book`, `symbol=fUSD/fUST`, `prec=P0`, `freq=F0`, configured `len`, and conf flags `SEQ_ALL | OB_CHECKSUM` (`196608`). It handles snapshot, update, heartbeat, subscription/error events, sequence frames, and checksum frames.

- [ ] **Step 1: Write failing parser and book-validity tests**

~~~python
def test_book_subscription_enables_sequence_and_checksum():
    client = FundingBookWSClient(symbols=("fUST",), length=25)
    frames = client.subscription_frames()

    assert json.loads(frames[0]) == {"event": "conf", "flags": 196608}
    assert json.loads(frames[1]) == {
        "event": "subscribe", "channel": "book", "symbol": "fUST",
        "prec": "P0", "freq": "F0", "len": 25,
    }


def test_invalid_checksum_makes_snapshot_unusable():
    store = FundingBookStore(max_age_seconds=30)
    store.apply_snapshot("fUST", _book_snapshot())
    store.apply_checksum("fUST", checksum=123, expected=456)

    assert store.snapshot("fUST", now_ms=1_000) is None


def test_exact_period_requires_depth_and_uses_absolute_bid_amount():
    snapshot = _snapshot(
        bids=[_level(rate="0.00020", period=7, amount="-100")],
        asks=[_level(rate="0.00021", period=7, amount="100")],
    )

    assert snapshot.exact_period(period_days=7, amount=Decimal("100")) is not None
    assert snapshot.exact_period(period_days=14, amount=Decimal("100")) is None
    assert snapshot.exact_period(period_days=7, amount=Decimal("101")) is None
~~~

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_book_ws.py tests/modules/marketfeed/test_funding_book.py -q`

Expected: FAIL because the WS frames, checksum state, and `MarketSnapshot` do not exist.

- [ ] **Step 3: Implement WS parsing and checksum/sequence state**

Create a separate `FundingBookWSClient`; do not add funding-book branches to the existing candle-only client. Compute the Bitfinex funding-book CRC32 over the top 25 bids descending and asks ascending using the documented `rate:period:amount` token order, convert the unsigned CRC32 to signed 32-bit before comparison, and mark the store invalid on any mismatch or sequence gap. On disconnect, clear the usable flag. On reconnect, require a complete WS snapshot plus a successful REST snapshot before setting `sequence_valid=True` and `checksum_valid=True`. REST errors do not invalidate an independently fresh valid WS snapshot; REST resync is mandatory after an invalid stream state.

- [ ] **Step 4: Implement freshness, exact-period and depth lookup**

`MarketSnapshot.is_fresh(now_ms, max_age_ms)` must reject future timestamps, stale timestamps, invalid sequence/checksum, and symbol mismatch. `FundingBookStore.snapshot()` returns no value for an unusable snapshot. `FundingBookService` periodically calls `BitfinexREST.get_funding_book()` for each configured symbol, applies the normalized REST snapshot, and keeps the historical `BookSnapshotWriter` path separate from the live eligibility provider.

- [ ] **Step 5: Run focused tests and lint**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_book_ws.py tests/modules/marketfeed/test_funding_book.py -q && uv run ruff check src/bfx_funding_bot/external/bitfinex/funding_book_ws.py src/bfx_funding_bot/modules/marketfeed/funding_book.py`

Expected: PASS with no network access; all frames are fed through fake socket/test methods.

- [ ] **Step 6: Commit the market-data boundary**

~~~bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/funding_book_ws.py backend_py/src/bfx_funding_bot/modules/marketfeed/funding_book.py backend_py/src/bfx_funding_bot/external/bitfinex/rest.py backend_py/tests/external/bitfinex/test_funding_book_ws.py backend_py/tests/modules/marketfeed/test_funding_book.py
git commit -m "✨ Feat: add reconciled period-aware funding book"
~~~

## Task 3: Add durable pre-trade execution audit

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/execution/audit/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/modules/execution/audit/tables.py`
- Create: `backend_py/src/bfx_funding_bot/modules/execution/audit/model.py`
- Create: `backend_py/src/bfx_funding_bot/modules/execution/audit/recorder.py`
- Create: `backend_py/tests/modules/execution/audit/test_recorder.py`
- Create: `backend_py/tests/modules/execution/audit/test_tables_metadata.py`
- Create: `backend_py/alembic/versions/f4a7c9d1e2b3_add_execution_decisions.py`
- Modify: `backend_py/alembic/env.py`

**Interfaces:**
- Produces `ExecutionDecision` with fields: `decision_id`, `account_id`, `deployment_environment`, `reconcile_id`, `cell_id`, `symbol`, `signal_correlation_id`, `outcome`, `reason_code`, `failed_dependency`, `signal_rate`, `applied_rate`, `amount_usdt`, `duration_days`, `snapshot_id`, `snapshot_hash`, `snapshot_captured_at_ms`, `snapshot_source`, `snapshot_age_ms`, `model_version`, `model_hash`, `model_evidence`, `safety_result`, `execution_policy`, `service_version`, `config_hash`, `occurred_at_ms`, and `recorded_at_ms`.
- Produces frozen `AuditContext(account_id: str, deployment_environment: str, reconcile_id: str, cell_id: str, symbol: str, signal_correlation_id: str, service_version: str, config_hash: str)`; `ExecutionGate` uses it to populate every audit row.
- Produces `ExecutionDecisionRecorder(session_factory).record(decision: ExecutionDecision) -> None`; it owns a transaction and raises `ExecutionAuditUnavailable` on insert/commit failure.
- Adds `execution_decisions` with `decision_id` primary key, append-only timestamps, bounded indexes on `(deployment_environment, occurred_at_ms)`, `(symbol, occurred_at_ms)`, and `(outcome, reason_code)`; no update/delete API is exposed.

- [ ] **Step 1: Write failing persistence/order tests**

~~~python
async def test_ready_decision_is_durable_and_contains_evidence(db_factory):
    recorder = ExecutionDecisionRecorder(db_factory)
    decision = _ready_audit_decision()

    await recorder.record(decision)

    async with db_factory() as session:
        row = await session.get(ExecutionDecisionRow, decision.decision_id)
    assert row is not None
    assert row.outcome == "ready"
    assert row.snapshot_id == "book-42"
    assert row.model_hash == "sha256:model"


async def test_audit_commit_failure_raises_typed_error(failing_factory):
    recorder = ExecutionDecisionRecorder(failing_factory)

    with pytest.raises(ExecutionAuditUnavailable):
        await recorder.record(_ready_audit_decision())
~~~

- [ ] **Step 2: Run tests to verify the schema and recorder are absent**

Run: `cd backend_py && uv run pytest tests/modules/execution/audit/test_recorder.py tests/modules/execution/audit/test_tables_metadata.py -q`

Expected: FAIL because the audit module/table/migration are not present.

- [ ] **Step 3: Implement the table, immutable record and recorder**

Use the existing `Base`, `async_sessionmaker[AsyncSession]`, and `session_scope` pattern from `modules/execution/event_store/persister.py`. `record()` must execute one insert and commit before returning; rollback and raise `ExecutionAuditUnavailable` on any exception. Serialize enum values as their stable strings. Do not send this record through `DiagnosticsSink` and do not use a best-effort event emitter as an audit substitute.

- [ ] **Step 4: Add and validate the Alembic migration**

Create revision `f4a7c9d1e2b3` with `down_revision = "d437f9d4e3fe"`, import `audit.tables` in `alembic/env.py`, and create the table/indexes with the Postgres JSONB/plain JSON test-dialect pattern already used by the repository. The downgrade drops only `execution_decisions` and its indexes.

Run:

~~~bash
cd backend_py
uv run alembic upgrade head
uv run alembic check
uv run pytest tests/modules/execution/audit/test_recorder.py tests/modules/execution/audit/test_tables_metadata.py -q
~~~

Expected: migration reaches the new head, `alembic check` is clean, and the recorder tests pass.

- [ ] **Step 5: Commit the durable audit boundary**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution/audit backend_py/alembic/env.py backend_py/alembic/versions/f4a7c9d1e2b3_add_execution_decisions.py backend_py/tests/modules/execution/audit
git commit -m "✨ Feat: persist pre-trade execution decisions"
~~~

## Task 4: Enforce the typed executor boundary and reservation correlation

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/protocols.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/events.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/schemas.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/emit.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py`
- Modify: `backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/paper.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/observability/tracing.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/observability/metrics.py`
- Modify: `backend_py/tests/modules/execution/test_paper.py`
- Modify: `backend_py/tests/modules/execution/middleware/test_reservation_emitting.py`
- Modify: `backend_py/tests/external/bitfinex/test_live_executor_pure.py`
- Modify: `backend_py/tests/modules/observability/test_tracing.py`

**Interfaces:**
- `ExecutorPort`, `EchoPaperExecutor`, `BitfinexLiveExecutor`, reservation middleware, metrics middleware, and tracing middleware all accept `ReadyToSubmit`.
- `ReservationIntent` gains required `execution_decision_id: str` before defaulted fields; its serialized event payload carries the same value.
- `OrderSubmitPayload` gains `execution_decision_id: str`; `emit_order_submit()` accepts `ready: ReadyToSubmit` and emits `ready.decision` plus the id.

- [ ] **Step 1: Write failing adapter contract tests**

~~~python
async def test_paper_executor_uses_the_rate_inside_ready_to_submit(paper_executor):
    ready = _ready_to_submit(rate="0.00023", decision_id="d-7")

    result = await paper_executor.submit(ready, _account_context())

    assert result.status == "filled"
    assert result.raw_response["offer_rate"] == "0.00023"


async def test_reservation_intent_links_execution_decision_id(reservation_middleware):
    ready = _ready_to_submit(decision_id="d-8")

    await reservation_middleware.submit(ready, _account_context(), cid=123)

    intent = reservation_middleware.last_intent
    assert intent.execution_decision_id == "d-8"
~~~

- [ ] **Step 2: Run the adapter tests and verify the old signature fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_paper.py tests/modules/execution/middleware/test_reservation_emitting.py tests/external/bitfinex/test_live_executor_pure.py tests/modules/observability/test_tracing.py -q`

Expected: FAIL because the adapters still expect `DecisionPayload` and `ReservationIntent` has no decision id.

- [ ] **Step 3: Change every submit implementation and wrapper**

Use `ready.decision` only at the venue/paper adapter boundary. The middleware must create its `ReservationIntent` with `execution_decision_id=ready.decision_id` before calling the wrapped executor. Update all `async def submit` implementations found by `rg -n "async def submit" backend_py/src`; do not leave a raw-decision compatibility branch. Update tracing/metrics delegation to pass the same immutable object without reconstructing it.

- [ ] **Step 4: Update event schemas and serialization tests**

Require `execution_decision_id` in `ReservationIntent` and `OrderSubmitPayload`; update event deserialization fixtures and every constructor. Assert a round trip preserves the id and that missing ids fail validation. Keep existing legacy ledger upcasters limited to historical event fields; do not create an upcaster that invents an execution decision id for new submits.

- [ ] **Step 5: Run focused tests and static checks**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_paper.py tests/modules/execution/middleware/test_reservation_emitting.py tests/external/bitfinex/test_live_executor_pure.py tests/modules/observability/test_tracing.py -q && uv run mypy src/bfx_funding_bot/modules/execution src/bfx_funding_bot/external/bitfinex/live_executor.py src/bfx_funding_bot/modules/observability`

Expected: PASS; no executor implementation accepts `DecisionPayload`.

- [ ] **Step 6: Commit the submit boundary**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py backend_py/src/bfx_funding_bot/modules/observability backend_py/src/bfx_funding_bot/modules/marketfeed/schemas.py backend_py/tests/modules/execution backend_py/tests/external/bitfinex/test_live_executor_pure.py backend_py/tests/modules/observability/test_tracing.py
git commit -m "♻️ Refactor: require validated submit decisions"
~~~

## Task 5: Replace scalar clamp with exact-period eligibility and integrate the reconciler

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/execution/deployment/period_pricing.py`
- Create: `backend_py/src/bfx_funding_bot/modules/execution/deployment/eligibility.py`
- Create: `backend_py/tests/modules/execution/deployment/test_period_pricing.py`
- Create: `backend_py/tests/modules/execution/deployment/test_eligibility.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py`
- Modify: `backend_py/tests/modules/execution/deployment/test_reconciler.py`
- Delete: `backend_py/src/bfx_funding_bot/modules/execution/deployment/book_clamp.py`
- Delete: `backend_py/tests/modules/execution/deployment/test_book_clamp.py`

**Interfaces:**
- Produces `PriceBranch(StrEnum)` with `taker`, `undercut`, `raise`, and `signal_floor`; it has no `fallback` value.
- Produces frozen `PriceDecision(rate: Decimal, branch: PriceBranch, evidence: Mapping[str, object])`.
- Produces `PeriodPricer(max_down_pct: Decimal, tick: Decimal).price(candidate: DecisionPayload, snapshot: MarketSnapshot) -> PriceDecision | BlockedExecution`; the candidate supplies signal rate, amount, duration and symbol so a blocked result always contains its candidate identity.
- Produces `ExecutionGate(policy: ExecutionPolicy, audit: ExecutionDecisionRecorder, readiness: TradingReadiness).prepare(candidate: DecisionPayload, *, decision_id: str, reconcile_id: str, snapshot: MarketSnapshot | None, price: PriceDecision | BlockedExecution, fill_evidence: FillModelEvidence | FillModelUnavailable | None, safety: GuardResult, audit_context: AuditContext) -> ReadyToSubmit | BlockedExecution | NoRecommendation`.
- `DeploymentReconciler` receives `FundingBookProvider`, `ExecutionGate`, and `ExecutionPolicy`; it no longer receives `FundingTicker`, `ClampPolicy`, or `clamp` settings.

- [ ] **Step 1: Write failing pricing and no-submit tests**

~~~python
def test_missing_exact_period_returns_period_not_found():
    candidate = _decision(
        symbol="fUST", offer_rate="0.00020", offer_amount_usdt="100",
        offer_duration_days=14,
    )
    result = PeriodPricer(max_down_pct=Decimal("0.15"), tick=Decimal("0.00000001")).price(
        candidate=candidate, snapshot=_snapshot_with_period(7),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.PERIOD_NOT_FOUND


async def test_book_failure_never_submits_original_quote(reconciler, executor, audit):
    await reconciler.deploy(_reconcile_without_valid_snapshot())

    executor.submit.assert_not_awaited()
    assert audit.last.outcome is DecisionOutcome.BLOCKED
    assert audit.last.reason_code is BlockReason.BOOK_STALE.value
~~~

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_period_pricing.py tests/modules/execution/deployment/test_eligibility.py -q`

Expected: FAIL because the old scalar clamp has no exact-period result or typed gate.

- [ ] **Step 3: Implement exact-period pricing**

Normalize the requested period against `MarketSnapshot` levels. First accept the exact-period bid only when `bid.rate >= signal_rate` and `abs(bid.amount) >= amount`; return the signal rate with branch `taker`. Otherwise require an exact-period ask with `ask.amount >= amount`, calculate `competitive = ask.rate - tick`, and return `signal_floor` at the signal rate when `competitive < signal_rate * (1 - max_down_pct)`, otherwise return `undercut` when `competitive >= signal_rate` or `raise` when it is below the signal rate. A missing exact period, invalid snapshot or insufficient depth returns a `BlockedExecution` with `period_not_found` or `insufficient_period_depth`; it never returns the input quote as an error result.

- [ ] **Step 4: Implement the gate and wire every candidate through it**

For each allocated quote, construct the signal `DecisionPayload`, evaluate the safety chain, obtain the exact-period snapshot, call `PeriodPricer`, and pass the typed result to `ExecutionGate`. `ExecutionGate` maps each failed dependency to a stable `BlockReason`, writes one `ExecutionDecision` for every candidate, and constructs `ReadyToSubmit` only after the READY audit commit returns. A safety failure is audited as `blocked/safety_guard_blocked`. An audit failure is logged as `ERROR`, updates readiness, and returns `blocked/execution_audit_unavailable` without invoking the executor.

Remove the `ticker_source` fetch, `FundingTicker` import, `ClampPolicy`, `clamp_rate`, `FALLBACK` log branch, and scalar `book_competitive` parameter. `_reprice_sweep` uses only its configured quote references; it must not consume ticker or infer an exact-period price. Delete `book_clamp.py` and its tests after all imports are removed.

- [ ] **Step 5: Verify the reconciler boundary**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_period_pricing.py tests/modules/execution/deployment/test_eligibility.py tests/modules/execution/deployment/test_reconciler.py -q && rg -n "FALLBACK|clamp_rate|FundingTicker|ticker_source|BFX_CLAMP" src tests`

Expected: targeted tests PASS and the `rg` command returns no matches under the active backend source/tests.

- [ ] **Step 6: Commit the fail-closed deployment path**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment backend_py/tests/modules/execution/deployment
git commit -m "♻️ Refactor: make deployment eligibility period-correct"
~~~

## Task 6: Add structured events, bounded metrics, readiness and daemon lifecycle wiring

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/marketfeed/readiness.py`
- Create: `backend_py/tests/modules/marketfeed/test_readiness.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/observability/metrics.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/observability/stdout_sink.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/healthz.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/admin/trading_status.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`
- Modify: `backend_py/tests/modules/observability/test_metrics.py`
- Modify: `backend_py/tests/modules/marketfeed/test_healthz.py`
- Modify: `backend_py/tests/modules/marketfeed/test_healthz_metrics.py`
- Modify: `backend_py/tests/modules/marketfeed/test_daemon_wiring.py`
- Modify: `backend_py/tests/modules/marketfeed/test_daemon_metrics_wiring.py`
- Modify: `backend_py/tests/modules/marketfeed/test_daemon_trading_status_wiring.py`

**Interfaces:**
- `TradingReadiness.set_ready()`, `set_blocked(reason: BlockReason, dependency: str)`, and `snapshot() -> ReadinessSnapshot` with `trading_ready: bool` and one bounded reason.
- Fixed event names are `funding.execution.eligibility`, `funding.execution.blocked`, `funding.execution.submitted`, `funding.book.snapshot_invalid`, and `funding.fill_model.unavailable`.
- Metrics methods add `observe_execution_decision(outcome, reason, policy)`, `observe_audit_persist_failure()`, `observe_book_snapshot(result)`, `observe_book_snapshot_age(age_seconds)`, `observe_execution_gate_duration(seconds)`, and `set_trading_ready(value)`.
- `GET /healthz` remains process liveness; `GET /readyz` returns `200` only when trading-ready and `503` with the bounded reason otherwise.

- [ ] **Step 1: Write failing readiness, event and metric tests**

~~~python
def test_readiness_block_does_not_change_process_liveness():
    readiness = TradingReadiness()
    readiness.set_blocked(BlockReason.BOOK_STALE, "market_snapshot")

    assert readiness.snapshot().trading_ready is False
    assert readiness.snapshot().reason == BlockReason.BOOK_STALE.value


def test_execution_metric_labels_are_bounded(metrics):
    metrics.observe_execution_decision(
        outcome="blocked", reason=BlockReason.BOOK_STALE.value,
        policy=ExecutionPolicy.BOOK_GUARDED.value,
    )

    sample = metrics.registry.get_sample_value(
        "bfx_execution_decisions_total",
        {"outcome": "blocked", "reason": "book_stale", "policy": "book_guarded"},
    )
    assert sample == 1


async def test_readyz_returns_503_for_business_blocked_state(client, readiness):
    readiness.set_blocked(BlockReason.FILL_MODEL_MISSING, "fill_model")

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["trading_ready"] is False
    assert response.json()["reason"] == "fill_model_missing"
~~~

- [ ] **Step 2: Run the tests and verify the observability interfaces are absent**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_readiness.py tests/modules/observability/test_metrics.py tests/modules/marketfeed/test_healthz.py tests/modules/marketfeed/test_healthz_metrics.py -q`

Expected: FAIL because readiness and the new metric/route names do not exist.

- [ ] **Step 3: Implement bounded observability**

Keep event identity and severity separate. Emit the fixed event name plus `decision_id`, `reconcile_id`, `symbol`, `cell`, `policy`, `outcome`, `reason_code`, timestamp and bounded evidence. Truncate exception text to a fixed diagnostic field or omit it; never emit secrets, credentials or raw book payloads. Metrics methods must catch their own client failures so telemetry cannot turn a blocked candidate into an exception path.

- [ ] **Step 4: Wire daemon dependencies and lifecycle**

Construct one `TradingReadiness`, `FundingBookService` for book-capable policies, `ExecutionDecisionRecorder`, and `ExecutionGate`. Run the book service in the existing `asyncio.TaskGroup`; close it during daemon shutdown. Paper mode may omit the venue book service, but `book_guarded`, `optimizer_shadow`, and `optimizer_live` must fail startup when the provider is absent. Keep `BookSnapshotWriter` as the historical recorder and do not use it as the current eligibility source.

- [ ] **Step 5: Verify liveness/readiness and wiring**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_readiness.py tests/modules/observability/test_metrics.py tests/modules/marketfeed/test_healthz.py tests/modules/marketfeed/test_healthz_metrics.py tests/modules/marketfeed/test_daemon_wiring.py tests/modules/marketfeed/test_daemon_metrics_wiring.py tests/modules/marketfeed/test_daemon_trading_status_wiring.py -q && uv run mypy src/bfx_funding_bot/modules/marketfeed/readiness.py src/bfx_funding_bot/modules/marketfeed/healthz.py src/bfx_funding_bot/modules/marketfeed/daemon.py src/bfx_funding_bot/modules/observability`

Expected: `/healthz` remains healthy while `/readyz` reports the business block; daemon task shutdown closes the book client.

- [ ] **Step 6: Commit observability and readiness**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/readiness.py backend_py/src/bfx_funding_bot/modules/marketfeed/healthz.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/src/bfx_funding_bot/modules/admin/trading_status.py backend_py/src/bfx_funding_bot/modules/observability backend_py/tests/modules/marketfeed backend_py/tests/modules/observability
git commit -m "✨ Feat: expose execution readiness and audit metrics"
~~~

## Task 7: Remove empirical-model and WFO/OOS fallback paths

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/lending/tracking/artifact.py`
- Create: `backend_py/alembic/versions/f5b8d0e2f3c4_add_fill_model_artifacts.py`
- Create: `backend_py/tests/modules/lending/tracking/test_model_artifact.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/lending/tracking/tables.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/lending/tracking/repository.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/lending/tracking/model.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/lending/tracking/fill_rate.py`
- Modify: `backend_py/scripts/learn_fill_rate.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/config.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/engine.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/schemas.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/matrix.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/oos_eval.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/cell_derivation.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/cell_pipeline.py`
- Modify: `backend_py/scripts/run_phase3b_wfo_matrix.py`
- Modify: `backend_py/scripts/run_oos_profitability.py`
- Modify: `backend_py/scripts/derive_cells.py`
- Modify: `backend_py/tests/modules/lending/tracking/test_fill_rate_model.py`
- Modify: `backend_py/tests/modules/lending/tracking/test_fill_rate_repository.py`
- Modify: `backend_py/tests/modules/backtest/test_engine_empirical_fill.py`
- Modify: `backend_py/tests/modules/backtest/test_oos_eval.py`
- Modify: `backend_py/tests/modules/backtest/test_matrix.py`
- Modify: `backend_py/tests/modules/backtest/test_cell_derivation.py`
- Modify: `backend_py/tests/scripts/test_learn_fill_rate.py`

**Interfaces:**
- `FillModelArtifact(symbol, period_agg, horizon_h, source, model_version, schema_version, artifact_hash, training_start_ms, training_end_ms, cutoff_ms, sample_count, confidence_min_samples)` is immutable and is persisted separately from bucket rows.
- `FillModelEvidence(fill_prob, expected_ttf_ms, n_samples, symbol, period_agg, horizon_h, model_version, artifact_hash, cutoff_ms)` is returned only for a high-confidence matching artifact.
- `FillModelUnavailable(reason: Literal["missing", "low_confidence", "scope_mismatch", "unversioned"])` is the only non-ready model result.
- `run_backtest(..., config: BacktestConfig, fill_model: FillRateModel | None)` requires an explicit config; empirical mode raises `BacktestIncomplete` on missing/low-confidence evidence, while `linear-baseline` calls `compute_fill_prob` only when explicitly selected.
- `evaluate_oos_windows(..., config: BacktestConfig, fill_model: FillRateModel | None)` and `derive_cell_params(..., config: BacktestConfig, fill_model: FillRateModel)` require the model/config arguments; no helper constructs a baseline config internally.
- `WindowOutcome.status` includes `incomplete`; `CellVerdict` and `StrategyVerdict` cannot qualify when any required window/cell is incomplete.

- [ ] **Step 1: Write failing model and WFO propagation tests**

~~~python
def test_unversioned_or_low_confidence_model_is_unavailable():
    model = FillRateModel.from_rows(_rows_without_artifact_hash(), artifact=None)

    result = model.estimate_fill(
        reference_rate=Decimal("0.00020"), offer_rate=Decimal("0.00021"),
        period_agg="p2", horizon_h=4,
    )

    assert isinstance(result, FillModelUnavailable)
    assert result.reason in {"unversioned", "low_confidence"}


def test_empirical_backtest_does_not_use_linear_when_model_is_missing(candles):
    with pytest.raises(BacktestIncomplete, match="fill_model_missing"):
        run_backtest(
            candles, _strategy(), BacktestConfig(fill_model="empirical"),
            fill_model=None,
        )


def test_incomplete_wfo_cannot_qualify():
    verdict = evaluate_cell_qualification([_incomplete_window()])

    assert verdict.qualifies is False
    assert verdict.incomplete_windows == 1
~~~

- [ ] **Step 2: Run the model/backtest tests and verify the old fallback is observed**

Run: `cd backend_py && uv run pytest tests/modules/lending/tracking/test_fill_rate_model.py tests/modules/backtest/test_engine_empirical_fill.py tests/modules/backtest/test_oos_eval.py tests/modules/backtest/test_matrix.py tests/modules/backtest/test_cell_derivation.py -q`

Expected: the pre-change empirical tests fail because the engine currently falls through to `compute_fill_prob` and the OOS helper supplies an implicit linear config.

- [ ] **Step 3: Add artifact metadata and versioned model loading**

Create revision `f5b8d0e2f3c4` with `down_revision = "f4a7c9d1e2b3"`. Add `fill_rate_model_artifacts` with a primary key on `artifact_hash`, scope columns, cutoff/range/sample metadata, schema/model versions and JSON metadata. Add `artifact_hash` to `fill_rate_stats`; rows without a matching artifact are rejected as `unversioned`. Make `learn_fill_rate.py` compute a deterministic SHA-256 over canonical sorted scope/config/stat rows, insert the artifact row, and write the same hash on every bucket row in the group. Define `FillRateModel.from_rows(rows, *, artifact: FillModelArtifact | None)` and require the matching artifact before validating symbol/period/horizon scope and interpolating.

- [ ] **Step 4: Make backtest config/model propagation explicit**

Rename the explicit research mode from `linear` to `linear-baseline` and update every call site, test and report label. Make `run_backtest` require `config`; in empirical mode, call only `FillRateModel.estimate_fill()` and raise `BacktestIncomplete("fill_model_missing" | "fill_model_low_confidence")` for an unavailable result. Add `model_kind`, `model_version`, `artifact_hash`, `model_cutoff_ms` and `incomplete_reason` fields to `BacktestResult`; the matrix window also carries the typed incomplete reason. Preserve `compute_fill_prob()` only as an offline baseline implementation.

- [ ] **Step 5: Thread the model through every WFO/OOS path**

Pass `config` and `fill_model` into baseline, train and test `run_backtest` calls in `run_cell_wfo`; pass them through `evaluate_oos_windows`, `derive_cell_params`, `cell_pipeline`, `run_phase3b_wfo_matrix.py`, `run_oos_profitability.py`, and `derive_cells.py`. Catch only `BacktestIncomplete` at the window boundary, record `status="incomplete"` with a stable reason, and make the aggregate verdict false whenever an expected window is incomplete. The matrix report must print `model_kind`, `model_version`, `artifact_hash`, `cutoff_ms`, sample counts and incomplete reasons.

- [ ] **Step 6: Run migration and model/WFO tests**

Run:

~~~bash
cd backend_py
uv run alembic upgrade head
uv run alembic check
uv run pytest tests/modules/lending/tracking tests/modules/backtest tests/scripts/test_learn_fill_rate.py -q
uv run mypy src/bfx_funding_bot/modules/lending/tracking src/bfx_funding_bot/modules/backtest
~~~

Expected: versioned empirical paths pass, missing/low-confidence paths are incomplete, and only explicit `linear-baseline` tests use the linear formula.

- [ ] **Step 7: Commit the model-contract refactor**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/lending/tracking backend_py/src/bfx_funding_bot/modules/backtest backend_py/scripts/learn_fill_rate.py backend_py/scripts/run_phase3b_wfo_matrix.py backend_py/scripts/run_oos_profitability.py backend_py/scripts/derive_cells.py backend_py/alembic/versions/f5b8d0e2f3c4_add_fill_model_artifacts.py backend_py/tests/modules/lending/tracking backend_py/tests/modules/backtest backend_py/tests/scripts/test_learn_fill_rate.py
git commit -m "♻️ Refactor: make empirical fill evidence explicit"
~~~

## Task 8: Add deterministic rate optimizer and shadow/live policy semantics

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/execution/deployment/rate_optimizer.py`
- Create: `backend_py/tests/modules/execution/deployment/test_rate_optimizer.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/deployment/eligibility.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/config.py`
- Modify: `backend_py/tests/modules/execution/deployment/test_eligibility.py`
- Modify: `backend_py/tests/modules/marketfeed/test_config.py`

**Interfaces:**
- Produces frozen `RateCandidate(rate: Decimal, source: Literal["signal", "maker", "taker"], fill_evidence: FillModelEvidence | None, book_evidence: Mapping[str, object])`.
- Produces frozen `OptimizationResult(selected: RateCandidate, candidates: tuple[RateCandidate, ...], scores: Mapping[str, Decimal], model_version: str, artifact_hash: str)`.
- Produces `RateOptimizer.select(signal_rate: Decimal, *, maker: RateCandidate | None, taker: RateCandidate | None, fill_evidence: FillModelEvidence, fee_rate: Decimal) -> OptimizationResult | NoRecommendation`.
- Candidate score is exactly `candidate_rate * fill_probability * (1 - fee_rate)`; candidates below the signal floor are excluded; ties prefer higher fill probability and then lower quote rate.

- [ ] **Step 1: Write failing optimizer and policy tests**

~~~python
def test_optimizer_selects_highest_expected_net_rate():
    result = RateOptimizer().select(
        signal_rate=Decimal("0.00020"),
        maker=_candidate("0.00021", "maker"),
        taker=_candidate("0.00020", "taker"),
        fill_evidence=_evidence(fill_prob="0.95"),
        fee_rate=Decimal("0.15"),
    )

    assert result.selected.source == "taker"
    assert result.scores["taker"] == Decimal("0.0001615")


async def test_optimizer_live_blocks_when_model_is_unavailable(gate):
    result = await gate.prepare(
        _candidate_decision(), decision_id="d-live", reconcile_id="r-1",
        snapshot=_fresh_snapshot(), price=_priced_signal(),
        fill_evidence=FillModelUnavailable("missing"),
        safety=_allowed_guard(), audit_context=_audit_context(),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.FILL_MODEL_MISSING


async def test_optimizer_shadow_does_not_change_submitted_signal_rate(gate, executor):
    result = await gate.prepare(
        _candidate_decision(rate="0.00020"), decision_id="d-shadow", reconcile_id="r-2",
        snapshot=_fresh_snapshot(), price=_priced_signal(),
        fill_evidence=_evidence(fill_prob="0.95"), safety=_allowed_guard(),
        audit_context=_audit_context(),
    )

    assert isinstance(result, ReadyToSubmit)
    assert result.decision.offer_rate == 0.00020
~~~

- [ ] **Step 2: Run optimizer tests and verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_rate_optimizer.py tests/modules/execution/deployment/test_eligibility.py -q`

Expected: FAIL because no candidate scoring or policy-specific model handling exists.

- [ ] **Step 3: Implement candidate scoring and availability semantics**

Generate only signal, exact-period maker and exact-period taker candidates. `optimizer_shadow` invokes the optimizer for logs/audit but passes the already book-guarded signal price to `ExecutionGate`; an unavailable shadow model emits `funding.fill_model.unavailable` and `NoRecommendation` telemetry for the optimizer branch while the independent book-guarded submit remains unchanged. `optimizer_live` requires a high-confidence evidence value and maps missing/low-confidence evidence to blocked; it never submits the signal as a hidden optimizer fallback. `paper` can run an explicit `linear-baseline` research scorer but has no venue authority.

- [ ] **Step 4: Persist optimizer evidence without high-cardinality labels**

Store selected source, candidate scores, model version/hash and exact-period evidence in the `execution_decisions.model_evidence` JSON for READY/BLOCKED records. Emit only the fixed event name and bounded policy/outcome/reason labels to metrics. Do not put candidate rates, cell ids, symbols or artifact hashes into Prometheus labels.

- [ ] **Step 5: Run targeted optimizer/config checks**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_rate_optimizer.py tests/modules/execution/deployment/test_eligibility.py tests/modules/marketfeed/test_config.py -q && uv run mypy src/bfx_funding_bot/modules/execution/deployment`

Expected: PASS; shadow preserves the active signal rate, live blocks unavailable evidence, and no optimizer result can call the executor directly.

- [ ] **Step 6: Commit optimizer semantics**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment backend_py/src/bfx_funding_bot/modules/marketfeed/config.py backend_py/tests/modules/execution/deployment backend_py/tests/modules/marketfeed/test_config.py
git commit -m "✨ Feat: add evidence-gated lending rate optimizer"
~~~

## Task 9: Finish profiles, remove configuration debt, update architecture docs and run the full gate

**Files:**
- Create: `deploy/vm/shadow-p14.env`
- Modify: `scripts/deploy-vm.sh`
- Modify: `deploy/vm/paper.env`
- Modify: `deploy/vm/shadow.env`
- Modify: `deploy/vm/canary.env`
- Modify: `.env.example`
- Modify: `backend_py/.env.example`
- Modify: `backend_py/ARCHITECTURE.md`
- Create: `backend_py/tests/modules/marketfeed/test_deploy_profile.py`
- Modify: `backend_py/tests/modules/marketfeed/test_canary_invariant.py`

**Interfaces/configuration:**
- `paper.env`: `BFX_EXECUTION_POLICY=paper` and no book/model live dependency.
- `shadow.env`: `BFX_EXECUTION_POLICY=book_guarded` for the normal shadow path.
- `shadow-p14.env`: `BFX_PHASE=shadow`, `BFX_DEPLOYMENT_ENV=shadow`, `BFX_EXECUTION_POLICY=optimizer_shadow`, `BFX_CELLS_YAML=/app/configs/cells.experimental-p14.yaml`, `p_mid=7`, `p_long=14`, `t1=0.5`, `t2=1.5` through the existing cell/profile mechanism.
- `canary.env`: explicit `BFX_EXECUTION_POLICY=book_guarded` or `optimizer_live` only after all required evidence settings are present; remove every `BFX_CLAMP_*` variable.
- `scripts/deploy-vm.sh shadow-p14` selects `deploy/vm/shadow-p14.env` but retains the existing canary confirmation/cap/safety checks.

- [ ] **Step 1: Write failing profile and static debt tests**

~~~python
def test_shadow_p14_is_shadow_only():
    env = parse_env_file(Path("deploy/vm/shadow-p14.env"))

    assert env["BFX_PHASE"] == "shadow"
    assert env["BFX_DEPLOYMENT_ENV"] == "shadow"
    assert env["BFX_CELLS_YAML"].endswith("cells.experimental-p14.yaml")
    assert env["BFX_EXECUTION_POLICY"] == "optimizer_shadow"


def test_deployment_profiles_contain_no_legacy_clamp_flags():
    for path in Path("deploy/vm").glob("*.env"):
        assert "BFX_CLAMP_" not in path.read_text()


def test_canary_profile_cannot_select_p14():
    assert "experimental-p14" not in Path("deploy/vm/canary.env").read_text()
~~~

- [ ] **Step 2: Run profile tests and verify the old profiles fail**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_deploy_profile.py tests/modules/marketfeed/test_canary_invariant.py -q`

Expected: FAIL because `shadow-p14.env` is absent and canary still contains clamp flags.

- [ ] **Step 3: Update profiles, deploy selection and source-of-truth documentation**

Add explicit policy/book/model variables to the examples and VM profiles. Remove all `BFX_CLAMP_*` variables and stale comments describing ticker clamp or fallback. Add the execution flow, audit ordering, exact-period book validity rules, `/readyz` contract, event names, metric labels and p14 shadow restrictions to `backend_py/ARCHITECTURE.md`. Keep `cells.canary.yaml` and `safety.canary.yaml` byte-for-byte unchanged.

- [ ] **Step 4: Run repository-wide active-path scans**

Run a repository search for the old clamp names, fallback branch name, ticker pricing dependency and unguarded linear-model calls. Expected: only the explicit offline `linear-baseline` implementation and historical prose that is deliberately retained in migration notes remain; no active deployment source, profile or test refers to a fallback branch or clamp flag. Remove any remaining active-path match before continuing.

- [ ] **Step 5: Run targeted suites and the required full verification**

Run:

~~~bash
cd backend_py
uv run pytest -m "not integration"
uv run mypy src/
uv run ruff check
uv run alembic check
~~~

Expected: all unit/non-integration tests pass, mypy and ruff report no errors, and Alembic reports no metadata drift. If a failure occurs, fix the implementation and add/adjust the smallest regression test before re-running the complete command; do not weaken a gate or restore a fallback.

- [ ] **Step 6: Review the final diff and commit only plan-scoped files**

Run:

~~~bash
git status --short
git diff --check
git diff --stat HEAD
~~~

Confirm the two pre-existing staged deletions are still present and untouched, and that no remote/deployment action was performed. Commit the implementation with the repository's emoji prefix after all verification commands pass:

~~~bash
git add scripts/deploy-vm.sh deploy/vm/paper.env deploy/vm/shadow.env deploy/vm/canary.env deploy/vm/shadow-p14.env .env.example backend_py/.env.example backend_py/ARCHITECTURE.md backend_py/tests/modules/marketfeed/test_deploy_profile.py backend_py/tests/modules/marketfeed/test_canary_invariant.py
git commit -m "🚀 Deploy: finalize fail-closed funding execution architecture"
~~~

---

## Spec Coverage Self-Review

| Spec requirement | Plan coverage |
|---|---|
| Typed `ReadyToSubmit`/`BlockedExecution`/`NoRecommendation` and no fallback | Tasks 1, 5, 8 |
| Explicit execution-policy enum and startup dependency checks | Tasks 1, 9 |
| WS funding book + REST reconcile + sequence/checksum/freshness/depth | Task 2 |
| Ticker excluded from exact-period pricing | Tasks 2, 5, 9 |
| Durable append-only audit before venue submit | Tasks 3, 4, 5 |
| `ReservationIntent` correlation | Task 4 |
| Fixed event names, bounded metrics and readiness/liveness split | Task 6 |
| Empirical artifact metadata and no runtime linear fallback | Task 7 |
| WFO/OOS/model propagation and incomplete aggregate handling | Task 7 |
| Deterministic optimizer, fee, signal floor, shadow invariance | Task 8 |
| p14 shadow-only profile and canary exclusion | Task 9 |
| No remote deployment or live parameter change | Global Constraints and Task 9 |

## Plan Self-Review Checks

Before handing this plan to an implementation session, perform a literal scan for unresolved placeholders and vague instructions. Expected: no matches. The shared types are introduced in Tasks 1–3 and reused with the same names/signatures in Tasks 4–9; the final full gate is explicitly listed in Task 9.
