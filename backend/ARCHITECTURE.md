# bfx-funding-bot 後端架構（Python / backend）

> Bitfinex 自動放貸 bot 的現行後端（`backend/`，Python 3.13）。本文件描述的是 **事件溯源（event-sourced）的長駐 daemon**：以 PostgreSQL event log 為單一真相源（SoT），透過 standing-quote 解耦「訊號決策」與「資金部署」，由 DeploymentReconciler 單一寫入者控制 venue 提交，並以週期性 reconcile 對齊 venue 真相作為正確性骨幹。
>
> Go `backend/` 已封存，本文件完全不涵蓋。所有路徑相對於 `backend/src/bfx_funding_bot/`。

---

## 1. Overview

bfx-funding-bot 在 Bitfinex 的 funding（放貸）市場上自動掛單放貸閒置資金。系統不是「來一根 candle 就送一張單」的反應式 bot，而是把整條 pipeline 拆成兩個解耦的時間軸：

- **訊號層（signal layer）**：每小時 candle boundary 觸發一次，由策略產生「該不該放、用什麼 rate、放幾天」的 **意圖（StandingQuote）**，寫進 in-memory store，**不送單**。
- **部署層（deployment layer）**：每 ~90 秒一次，先用 REST 把 venue 真相抓回來校正 ledger，再依「目標曝險 − 目前曝險」的缺口（gap）把資金貪婪分配到各 cell，套用 safety chain 後才送單。

核心架構原則：

1. **Event-sourced execution，PG 為 SoT**：所有狀態變更（`ReservationIntent` / `ReservationClaimed` / `OrderFilled` / `ReservationReleased` / `ReservationFailed`）都 append 進不可變的 `event_log`。`position_state`、`offer_claims` 等都是可由 log 重算的投影（projection）。
2. **訊號 / 部署解耦（standing quote）**：rate 決策在 1h 尺度上穩定，資金分配在 90s 尺度上靈敏；StandingQuote 以 ~65 分鐘 TTL 橋接兩者。
3. **單一寫入者曝險（single-writer exposure）**：`DeploymentReconciler` 是 venue 提交的唯一寫入者；訊號層不送單。
4. **Reconcile 即正確性骨幹（reconcile-as-correctness-backbone）**：venue（透過 REST）是曝險真相的權威來源；90s 週期 reconcile 保證 ledger 收斂。WebSocket 只是延遲最佳化（latency optimization），不是正確性來源。

部署形態：單一長駐 daemon，以 `asyncio.TaskGroup` 並行跑多個 sub-task；全 stack 自托於單一 Oracle Cloud VM（下稱 `<vm-host>`；docker compose：`bfx-bot`/`bfx-webapi`/`bfx-postgres`(PG 18)/`bfx-redis`/`bfx-frontend`）。Koyeb/Neon/Upstash 為歷史平台（2026-05-31 Koyeb cutover、2026-06-23 Neon/Vercel 歸零）。

### Release 0 web API containment and Halt 1 identity

Release 0 沒有 self-service signup；所有 private `/api/v1` route 仍僅接受
`BFX_OPERATOR_USER_ID` 指定的 Better Auth user，且 JWT role 必須是 `admin`
（`BFX_OPERATOR_ROLE=admin`）。operator ID 缺失或空白、user ID 不符、或 role
不符時一律 fail closed；不得 fallback 到第一個 user、環境變數 realm 或 user
profile。

Halt 1 後，money-domain 的 aggregate root 是 immutable
`ExchangeAccount.id`（UUID），不是登入 user、credential row 或 process-global
realm。private account routes 必須使用
`/api/v1/exchange-accounts/{exchange_account_id}/...`，dependency 依序驗證
JWT operator、UUID path、membership/lifecycle，再進入 handler；不存在的帳號與
沒有 membership 的帳號都回 non-enumerating 404。daemon 只接受
`BFX_EXCHANGE_ACCOUNT_ID`，啟動時載入 active account、credential 與 config
draft；缺少、未知、retired 或 halted account 都 fail closed。

`account_id` text 欄位在 Halt 1 contract migration 後只保留為 immutable
legacy/audit provenance；新讀寫的 owner/filter 是 `exchange_account_id`，新
domain event 以 canonical lowercase UUID string 相容既有 event contract。credentials
的 AEAD AAD 一律是 canonical UUID，絕不使用 user id 或舊 realm。

FastAPI 的 process liveness 與 database readiness 是兩個獨立契約：

- `GET /health`：process 仍能服務 request 時永遠回 `200 {"status":"ok"}`，不做 DB probe。因此 DB 初始化失敗後 lifespan 保留 app serving，liveness 仍可用。
- `GET /ready`：僅在 `app.state.session_factory` 存在、以獨立 read-only session 成功執行 `SELECT 1`，且 `alembic_version` 的 revision set 與 image 內 `alembic.ini` migration graph 的所有 head 完全相等時回 `200 {"status":"ready","checks":{"database":"ok"}}`。factory 缺失、migration table 缺失／空值／stale／多餘 head、SQLAlchemy/timeout/socket error 或 migration graph 無法讀取，均回 `503 {"status":"not_ready","checks":{"database":"failed"}}`，不洩漏 connection string。probe session 一律關閉、不 commit application data；timeout 由 `BFX_READINESS_TIMEOUT_SECONDS` 控制，預設 2.0 秒，最大固定為 10 秒（超過時 clamp）。

---

## 2. Layered Architecture

```mermaid
graph TD
    BFX[Bitfinex REST + WebSocket]

    subgraph signal["訊號 / 策略層 (1h boundary)"]
        SCHED[Scheduler]
        SE[SignalEngine.process_candle]
        REG[StrategyRegistry / build_strategy_at_boundary]
        STRAT["MeanReversion / RatePercentile"]
        DIV[DivergenceReporter]
        LOCF[reindex_and_ffill LOCF]
    end

    SQ[(StandingQuoteStore<br/>in-memory, TTL 65min)]

    subgraph deploy["部署層 (90s reconcile)"]
        PR[PeriodicReconcile.run_loop]
        BR[BootRecovery.run]
        DR[DeploymentReconciler.deploy]
        SIZE[sizing.allocate_gap]
        TRK[CellDeploymentTracker]
        SAFE[SafetyGuardChain]
    end

    subgraph exec["執行 middleware (single-writer)"]
        HB[HeartbeatMiddleware]
        RE[ReservationEmittingMiddleware A2]
        TR[TransientRetryMiddleware]
        LX[BitfinexLiveExecutor]
    end

    LED[(PaperPositionLedger<br/>reserved + realized)]
    BUS[DomainEventBus]

    subgraph sot["Event Store SoT (PostgreSQL)"]
        STORE[PostgresEventStore]
        ELOG[(event_log)]
        PS[(position_state)]
        OC[(offer_claims)]
        RO[(reconcile_observation)]
        DIAG[(diagnostics)]
    end

    WS[BitfinexLiveWSDispatcher]
    FT[RestPollingFillTracker]
    HP[HealthProbe / HealthMonitor]

    SCHED --> SE
    SE --> REG --> STRAT
    SE --> DIV
    REG --> LOCF
    SE --> SQ

    PR --> BR --> BFX
    PR --> DR
    DR --> SQ
    DR --> SIZE
    DR --> TRK
    DR --> SAFE
    SAFE --> HB --> RE --> TR --> LX --> BFX

    RE --> STORE
    BR --> STORE
    STORE --> ELOG
    STORE --> PS
    STORE --> OC
    STORE --> RO
    SAFE --> DIAG
    SE --> DIAG

    RE --> BUS
    BR --> BUS
    WS --> BUS
    BUS --> LED
    BUS --> REG
    LED --> SIZE
    LED --> TRK
    LED --> SAFE

    BFX -.WS frames.-> WS
    BFX -.REST poll.-> FT
    WS --> STORE
    WS -.request_resync on reconnect or seq gap.-> PR

    HP --> SAFE
    PR --> HP
    WS --> HP
```

依賴方向重點：訊號層**只寫** StandingQuoteStore；部署層**讀** quote、ledger（曝險真相）後才提交；ledger 由 venue reconcile（REST）與 WS 共同更新，但 reconcile 才是收斂保證；event store 在所有路徑下游，是唯一 durable 真相。

---

## 3. Data Flow

### 3a. 1h Boundary Path（訊號 → StandingQuote）

```mermaid
sequenceDiagram
    participant SCH as Scheduler
    participant SE as SignalEngine
    participant ST as Strategy (live instance)
    participant DIV as DivergenceReporter
    participant SQ as StandingQuoteStore
    participant DG as DiagnosticsSink

    Note over SCH: now >= next_candle_close_mts + buffer_s
    SCH->>SE: process_candle(cell, candle)
    SE->>ST: ExtractedSignal.extract → observe(candle) + decide(candle)
    ST-->>SE: LendDecision{rate=close, period_days=2} or None
    SE->>DIV: check() → rebuild via build_strategy_at_boundary (LOCF, history[:-1])
    DIV-->>SE: None | divergence_detail (direction flip)
    SE->>DG: emit SIGNAL (+ SIGNAL_DIVERGENCE warn) + DECISION (durable)
    SE->>SQ: update(StandingQuote{cell_id, POST/SKIP, rate, period, scid, created_at_ms})
    Note over SQ: TTL ~65min — 不送單，僅記錄意圖
```

### 3b. 90s Deployment Path（reconcile → ledger 真相 → gap → allocate → safety → submit → intent）

```mermaid
sequenceDiagram
    participant PR as PeriodicReconcile
    participant BR as BootRecovery
    participant BFX as Bitfinex REST
    participant ST as PostgresEventStore
    participant LED as PaperPositionLedger
    participant DR as DeploymentReconciler
    participant SQ as StandingQuoteStore
    participant BK as FundingBookService
    participant SA as SafetyGuardChain
    participant EG as ExecutionEligibility
    participant AD as ExecutionDecisionStore
    participant EX as Executor middleware

    Note over PR: 每 ~90s 一 tick（min 10s debounce）
    PR->>BR: run()
    BR->>BFX: GET /funding/offers + /funding/credits
    BFX-->>BR: venue snapshot
    BR->>BR: full-account REST snapshot（不套 symbol filter）
    BR->>BR: compute_recovery_actions (foreign→alert only, missing→RELEASED, stale PENDING→UNKNOWN)
    BR->>ST: append(recovery events + VENUE_SNAPSHOT_OBSERVED) [1 txn]
    ST->>ST: upsert venue_offer/credit_state + position buckets + reconcile_observation
    BR-->>LED: derived PositionReconciled → 絕對覆寫 (reserved, realized)
    PR->>PR: 比對 drift → 有變化即 RECONCILE DEGRADED + WARN
    PR->>DR: deploy()
    DR->>LED: reserved_exposure()
    DR->>DR: tracker.reconcile_to_total(reserved)  (僅用 reserved，不含 realized)
    DR->>DR: allocate_gap(target=cap, current=current_exposure)  greedy emptiest-first
    loop each fill
        DR->>SQ: get_active(cell_id, now_ms)  (POST 且未過 TTL)
        DR->>BK: current MarketSnapshot(symbol, period_days, amount)
        BK-->>DR: valid exact-period book evidence | typed unavailable
        DR->>SA: evaluate(decision)  (短路於第一個 BLOCK，2s/guard fail-closed)
        SA-->>DR: allowed
        DR->>EG: prepare(candidate, snapshot, safety)
        EG->>AD: append READY / BLOCKED decision（先於 venue）
        AD-->>EG: committed decision id
        EG->>EX: submit(ReadyToSubmit)（READY only）
        EX->>ST: txn1 ReservationIntent(PENDING) → REST → txn2 ReservationClaimed(+OrderFilled)
        DR->>DR: tracker.record_deploy(cell_id, amount)  (僅 acknowledged 才記)
    end
    PR->>PR: record heartbeat("periodic_reconcile")
```

### 3c. Fill Lifecycle（WS foc → OrderFilled；REST reconcile → PositionReconciled）

```mermaid
sequenceDiagram
    participant BFX as Bitfinex WS
    participant WS as BitfinexLiveWSDispatcher
    participant ST as PostgresEventStore
    participant BUS as DomainEventBus
    participant LED as PaperPositionLedger
    participant REG as OfferRegistry
    participant PR as PeriodicReconcile

    Note over BFX,WS: 低延遲路徑 (WS)
    BFX->>WS: foc EXECUTED frame (venue_seq)
    WS->>WS: translate_bfx_event → OrderFilled (+ CLAIMED→RELEASED)
    WS->>ST: _persist_then_publish — PG 先寫 (dedup on venue_seq) 再 publish；deduped 則 skip publish
    ST-->>BUS: OrderFilled
    BUS->>LED: on_order_filled → reserved -= delta, realized += size
    BUS->>REG: handle → CLAIMED → RELEASED
    Note over WS,PR: 單筆 fill 不觸發 resync，只有 auth WS 重連或 seq gap 才 request_resync（見 3d）

    Note over PR: 正確性骨幹 (REST, 每 90s)
    PR->>ST: append VENUE_SNAPSHOT_OBSERVED（full-account）— 絕對覆寫
    ST-->>BUS: PositionReconciled
    BUS->>LED: on_position_reconciled → 絕對 set (吸收 WS 漏接的 delta)
```

關鍵差異：**WS 是增量（delta）**（`reserved -= / realized +=`，floor at 0）；**REST reconcile 是絕對覆寫**（直接 set venue 真相）。即使 WS 完全靜默，90s reconcile 仍把 ledger 拉回正確值——這就是「reconcile 即正確性骨幹」的含義。

### 3d. Reconcile 骨幹規則（`execution/periodic_reconcile.py`、`execution/boot_recovery.py`）

- **Grace 時間**：`compute_recovery_actions` 有兩個窗口。`grace_ms`（boot 與 runtime 皆 120 s）決定 PENDING intent 多老才轉 UNKNOWN；`action_grace_ms` 決定 local CLAIMED 在 venue 缺席多久才發 `ReservationReleased(missing_from_venue)`——boot 為 0（立即），runtime reconcile 為 120 s（`apps/bot.py` 的 `runtime_recovery`），避免 venue snapshot 落後於剛掛出的 offer 而誤釋放。
- **EXECUTOR DOWN**：`PeriodicReconcile` 連續 3 次（`max_consecutive_failures=3`）venue fetch 失敗 → `HealthTarget.EXECUTOR=DOWN`，由 `AuthHealthGuard` 擋新單（不在過期 ledger 上交易）；下一次成功即自行清除。reconcile 迴圈絕不讓例外打掛 daemon。
- **Resync 觸發**：只有 authenticated WS（`external/bitfinex/auth_ws.py`）呼叫 `request_resync`：非首次連線（`reconnect`，含 venue 以 20051/20061 要求重連後的那次）與 SEQ_ALL 序號 gap（`seq_gap`）。首次連線由 boot reconcile 涵蓋。多次請求合併成一次，並受 `BFX_RESYNC_MIN_INTERVAL_S`（預設 10 s）debounce；單筆 fill 不觸發 resync。
- **Snapshot acceptance（`CapitalRepository.begin_snapshot` / `accept_snapshot`）**：venue I/O 前先在短 transaction 持久化 `capital_snapshot_queries` 的 command fence（當下 event head；有未完成 submission attempt 時拒絕開始，`snapshot_inflight_command`）。acceptance 要求：fence 未變（`snapshot_command_fence_changed`）、這是最新一筆 query（`snapshot_query_superseded`）、主觀測與**時間上不重疊的第二次觀測**（confirmation 的 query start ≥ 主觀測的 query finish）內容**完全相同**（否則 `snapshot_unstable`），兩者都通過 freshness／完整性驗證。只有被接受的 snapshot 才寫 `capital_snapshots`，也只有它們由 `LedgerConservation` 判斷借出額（fill 落在 offers 與 credits 兩個 request 之間會把同一筆錢算兩次，兩次相同觀測排除這種假象）。

### 3e. Bot 依 capital authority 組裝（`apps/bot_ports.py`）

資料庫的 capital authority epoch（`legacy` / `ledger`）在 `build_daemon` 讀一次，`select_bot_ports(authority, ...)` 一次選定 bot 行程每個 consumer 綁定的 adapter（web API 對應 `apps/read_models.py`）；`apps/bot.py` 其餘部分不指名 authority。可接受的 authority 不是 build 全域常數，而是各行程自己的集合（`apps/authority_support.py`）：Bitfinex venue 只收 `legacy`（S1-7 之前）、simulated venue 只收 `ledger`、web API 只收 `legacy`、`scripts/amend_capital_policy.py` 依資料庫的 realm stamp（`prod` 只收 `legacy`，`shadow`／`ci` 兩者皆收）。`read_authority(session, *, supported)` 由呼叫端傳入集合。Bitfinex venue 上的 ledger 組裝仍只由測試以 monkeypatch epoch 讀取進入；simulated venue 則以真實 epoch 列進入（見 §8「Venue 軸」）。

- **`BotPorts`**：`uncertainty_reader`、`managed_offers`、`venue_hint_sink`（一個實例；`select_bot_ports` 收到先建好的 `ResyncChannel`，`PeriodicReconcile`、auth WS 與 ledger hint sink 共用它）、`capital: CapitalPorts`（必有；`select_bot_ports` 沒有 `live` 參數，venue 由 `apps/venue.py` 另外決定，兩個 venue 共用同一組 ports）與 `legacy: LegacyExtras | None`。`CapitalPorts` 全有或全無：`capital_authority`、`scope_lock`、`policy_store`、`command_boundary`（journal + effects）、`operator_resolution`、`deployment_input`、`observation`（取得 venue 連線後建出 boot 與 runtime 兩個 sink，時間常數為 `ledger.BOOT_GRACE_MS`=0 / `RUNTIME_GRACE_MS`=120 000，attempt 與 foreign-offer grace 共用）、`uncertainty_synced`（legacy 才有：reconciler 在 durable pre-sizing guard 放行後收斂 in-memory uncertainty cache）。legacy 的 `PaperPositionLedger` / `OfferRegistry` bus 訂閱在 `_legacy_ports` 內建立（每個事件型別先 projection 後 registry）。REST fill tracker 只在 legacy 組裝。
- **legacy**：與原本相同的物件圖（`CapitalRuntime`、`LegacyCommandJournal` / `LegacyCommandEffects`、`BootRecovery` 包在 `LegacyObservationSink`、`EventStorePersister`、`PaperPositionLedger`、`OfferRegistry` 與其 bus 訂閱）。
- **ledger**：不建立 `CapitalRuntime`、`CapitalRepository`、`PostgresEventStore`、`EventStorePersister`、`PaperPositionLedger`、`OfferRegistry`、legacy hint sink。唯一紀錄是 ledger 自己的表；bus 只在寫入 transaction commit 之後承載通知（`CommandOutcomeNotice`、`UnknownResolutionNotice`、`VenueHintNotification`、`PositionReconciled`）。observation sink 是 `ledger.wiring` 的 cycle 外包 `LedgerCycleEffects`（保護、NAV、告警），不得包住 legacy sink。
- **Policy store（`ledger.PolicyStore`）**：`select_policy_ports(authority, ...)`（bot 與 `scripts/amend_capital_policy.py` 共用；腳本先讀 epoch）選 store 與 scope lock。兩個 authority 共用唯一寫入者 `ledger.policy_write.write_policy_revision`，呼叫端持有 scope lock，store 不代鎖。`read_applied` / `apply_policy` 讀寫兩個 authority 共用的 `capital_policy_heads` / `capital_policy_revisions`。legacy 實作先 replay event stream（`prepare_locked`），ledger 實作不碰 event stream。boot 的 policy 預檢與 `CapitalPolicyRequestWorker`（經 `accounts.capital_amendment`）都走它。
- **Boot 規則（`Daemon._run_boot_recovery`，與 authority 無關）**：`query_admission_refused` 擲 `BootInvariantError`（grace 0 且持有 writer lock，所有無 outcome 的 attempt 都已被 `close_dangling` 關閉，不應發生）；`fenced` / `incomplete_or_unequal` 記 WARNING、`periodic_reconcile.note_boot(decision)` 並繼續（無 accepted basis，交易由 `snapshot_query_pending` 擋下）；`accepted` 繼續。legacy sink 永遠回 `accepted`，行為不變。

---

## 4. 放貸演算法（End-to-End Lending Algorithm）

**Capital-policy live authority（2026-09）**：live 使用已套用且 versioned 的
`CapitalPolicy`、完整且 fresh 的 canonical venue snapshot 與未反映 commitments；
`CapitalRuntime`/`CapitalBudget` 共用 Decimal authority，planner、guard、command
boundary、status 不各自重算 cap。帳戶 spendable 扣除 reserve 與 commitments；
cell_limit 為 `max(0, total_capital - reserve) ×0.70`，cell_headroom 扣除該 cell 的歸屬曝險（offers、未反映 commitments 與歸屬該 cell 的 active credits）；無法歸屬的 credits 只計入 total_capital，不計入任何 cell，單筆上限取
spendable/headroom 的最小值。只有一個 active cell 也不放寬為100%。USD 明確
disabled，但所有 USD wallet/offers/credits 仍納入 full-account reconciliation。
未知/過期 snapshot、pending query、缺少 policy 全部 block，無 env/YAML fallback。

**授權與稽核分離（2026-09-20）**：授權讀取（每筆決策、`capital_policy` guard，預算
`GUARD_EVAL_TIMEOUT_SECONDS=2.0s`）**結構上有界**，只 fold accepted fence 之後的承諾；
稽核（全量重推整段歷史）留在沒有時間預算的地方。fence 以下的承諾不可能改變答案——
snapshot 不是已計入就是讓 fold 拒絕；重掃它們問的是完整性問題，屬於稽核。

讓讀取有資格跳過已證明的前綴，靠三件在 **acceptance** 決定並持久化的事實：

1. `capital_snapshots.covered_prefix_hash` — 該 classification 推導自哪段 ledger 前綴。
   驗證是免費的：讀取本來就會載入該 evidence event，而 `event_log.prefix_hash` 由
   `AccountEventWriter` 在唯一 append 入口以 rolling hash 維護（`H(prev ‖ record)`），
   所以驗證一段前綴只需讀一列，不是重算整段。NULL 視為**未證明**而非不存在。
2. 每筆 legacy intent 已被證明在 fence 或之前終結（`_assert_historical_intents_settled`）。
   少了它，仍被佔用的資金會被讀成可動用。**prefix hash 蓋不到這件事**——它偵測「前綴被竄改」，
   這裡的問題是「前綴從未被證明已結清」。
3. `classification["settled"]` — 未花費即結束的 attempt。少了它，已結清的 attempt 在尾端
   沒有對應 intent，會被誤報成未計入的承諾。

acceptance 發現歷史未結清時**記錄裁決而非拒絕觀測**
（`capital_snapshots.authorization_blocked_reason`，讀取以相同錯誤碼 fail closed）。
拒絕記錄觀測會連帶擋掉「解開該狀態所需的觀測」，會死鎖自己的復原。

**通則**：任何需要走訪歷史的判斷都在 acceptance 做完並存下結論，授權路徑只讀結論。
把這類判斷留在授權路徑上，等於讓工作量隨歷史成長，而 fail-closed timeout 會把它
**無聲地**變成「全部擋掉」——2026-09-20 即如此（`capital_policy` 2.31s vs 2.0s 預算，
每張單被擋，第一個訊號就是全面阻斷）。因此 guard 用掉 `GUARD_EVAL_WARN_FRACTION`
預算即記錄 `guard_slow` 並**仍放行**；timeout 是病態偵測，不是正確性邊界。
以下舊 gap/tracker 步驟僅說明 simulation helper／歷史演算法，不是 live 金額權限。

逐步流程（一個完整 decision-to-redeploy 週期）：

**訊號（每 1h candle boundary，每 cell 各一）**

1. Scheduler 在 `next_candle_close_mts + buffer_s` 觸發 `SignalEngine.process_candle`。buffer 是為了吸收 Bitfinex candle 落 DB 的延遲（否則讀到 0 列誤判 health degraded）。**buffer 不負責保證 candle 已定稿**——定稿由 `is_final` 認定（`CandleWriter` 見到更晚的 mts 才封存前一根），策略讀取路徑一律 final-only。2026-07-27 前靠 buffer 猜定稿時間（5s→30s），導致策略吃進成形中的值、live EMA 對 replay 漂移 4.1e-2（見 I-CI）。
2. `ExtractedSignal.extract` 呼叫 `strategy.observe(candle)` 更新狀態，再 `strategy.decide(candle)`：
   - **MeanReversionStrategy**：增量更新 EMA（`alpha = 2/(ema_span+1)`），`decide` 在 `close >= EMA - threshold_sigma * ratio_sigma` 時回 `LendDecision(rate=close, period_days=2)`，否則在下跌段暫停放貸。
   - **RatePercentileStrategy**：`observe` 把 `close` 推進 `deque(maxlen=lookback_hours)`，`decide` 在 window 填滿且 `close >= percentile(window, P)` 時放貸。
3. `DivergenceReporter.check()` 用 `build_strategy_at_boundary`（LOCF 重建，觀察 `history[:-1]`）重算 reference signal，與 live 比對 `signal_score` / `signal_direction` / `strategy_attributes`（含 EMA 等累加器內部 state，累加器欄位走 `_REL_TOL=1e-4` 相對容忍，其餘精確比對）。有差則 emit `SIGNAL_DIVERGENCE` warn。I-CI 之後 live 與 replay 應 byte-match，**觸發率脫離 100% 是這條修復的驗收指標**。
4. 結果寫成 `StandingQuote{cell_id, outcome=POST/SKIP, rate, period_days, signal_correlation_id, created_at_ms}` 進 `StandingQuoteStore`。**訊號層到此為止，零提交。**

**部署（每 ~90s，由 PeriodicReconcile 在 venue reconcile 完成後呼叫）**

5. `tracker.reconcile_to_total(ledger.reserved_exposure())`：把各 cell 的 in-memory 部署意圖等比例 rescale 到 **reserved（不含 realized）** 總量。
6. `allocate_gap(target=cap, current=current_exposure)`：
   - gap = cap − current_exposure（current_exposure = reserved + realized）；
   - **greedy emptiest-first**：依目前各 cell 已部署量由小到大排序，先填最空的；
   - simulation helper 每 cell 上限固定 `concentration_pct * target`，預設70%；live 則讀 canonical CapitalBudget，不使用此 target/cap helper；
   - 低於 `effective_min_usdt = ceil(150 * 1.02) = 153` USDT 的零頭（dust）丟棄；總分配 ≤ gap，全域 cap 永不超過。
7. 逐 fill：讀 `get_active(cell_id)`（須 POST 且未過 ~65min TTL，過期則跳過）→ 讀取同一 symbol、**exact `period_days`** 與所需 amount 的 `MarketSnapshot` → `SafetyGuardChain.evaluate` → `ExecutionEligibility.prepare`。scalar ticker 僅可作 telemetry，不能為 period-correct pricing 提供證據。

7b. **Reprice sweep（E1）**：allocation 前，對每個 symbol 比對 venue snapshot 的 resting offers 與現行 active quote：offer rate 高於最高 active quote rate ×(1+`BFX_REPRICE_TOLERANCE_PCT`) 且齡 ≥ `BFX_REPRICE_MIN_AGE_S` → `executor.cancel`（每 tick ≤ `BFX_REPRICE_MAX_CANCELS_PER_TICK` 筆；`BFX_REPRICE_ENABLED=false` 時僅 log `reprice_would_cancel`）。release 由 WS foc / 下次 reconcile 收斂，釋放資金下一 tick 以新 quote 重掛。無 active quote 的 symbol 不砍（resting 高價單留作 spike option）。**參考價**由 `BFX_REPRICE_REFERENCE` 決定：`quote`（預設）＝該 symbol active quote 最高 rate；`book`＝以 `PeriodPricer` 對當下 exact-period book、同期限、該 offer 剩餘金額重定價，多個 quote 取最高，book 或對應期限 quote 不可用時不砍——送單價本來就由 book 定，參考價用 signal quote 會在市場沒動時把合法價位砍掉（live 自 2026-09-27 起用 `book`，見 `deploy/vm/live.env`）。

7c. **Execution eligibility（fail-closed）**：snapshot 必須具備交易所交付的**完整 book baseline**（WS subscribe 時的 snapshot，或一次成功的 REST reconcile——兩者都是同一個交易所的整本 book，地位相同）、自該 baseline 以來**未偵測到 sequence gap 或 checksum mismatch**、交易所最後一次確認（update／`cs`／heartbeat）在 `BFX_BOOK_MAX_AGE_SECONDS` 內、symbol 相符、存在 exact period level，且該 side 的絕對 depth 足以覆蓋 amount。任一條件不足，或 model/safety/audit 不可用，皆產生 `BlockedExecution`／`NoRecommendation` 並不送單；book 不可用時 reason 必須指明是 `book_not_initialized`／`book_sequence_invalid`／`book_checksum_invalid`／`book_stale` 中的哪一種，不得塌縮成單一值（塌縮正是 checksum 缺陷被誤讀成過期的原因）。不得重用 signal quote、ticker 值或 linear estimate。`book_guarded` 以 exact-period book 價格送單；`optimizer_shadow` 只記錄候選與評分，仍送 book-guarded rate；`optimizer_live` 僅在 empirical evidence、fee 與其餘依賴全部有效時才可選擇 optimizer rate。

7c-i. **Public funding-book 協定事實**（皆經 live 量測確認，2026-09-21）：

- **checksum token 是 `RATE:AMOUNT`，不含 PERIOD**；數值採 JSON number 的最短往返表示（整數不帶 `.0`）。bids（amount<0）依 rate 降冪、asks（amount>0）依 rate 升冪，各取 25 筆後 bid/ask 交錯，CRC32 取號誌值。任一處偏離都讓每一筆 `cs` 永久不符，book 因此永不可用。
- **`cs` frame 由交易所自行排程**，間隔極不規律（fUST 實測 240 秒內 4 筆：128.9s／30.9s／5.1s）。因此 checksum 是**事後稽核**，不是送單前置條件；把它當前置條件會讓 book 絕大多數時間不可用。
- **SEQ_ALL 的序號屬於整條連線**，跨所有已訂閱 channel 單調遞增（實測 fUSD:snap=1、fUST:snap=2、fUSD:hb=3、fUST:hb=4）。連續性必須在連線層級檢查；若按 symbol 各自檢查 +1，多幣別訂閱會把每隔一筆都讀成 gap。gap 代表整條連線漏訊息，所有 symbol 的 book 同時失效。
- **WS snapshot 只在 subscribe 當下送一次**。失效後要重新取得完整 book，只能靠 REST reconcile 或重新訂閱；要求「再來一次 WS snapshot」等於要求一個交易所不會自己送出的東西。
- **heartbeat 是「內容未變」的明示確認**。安靜的 funding book 可以數分鐘沒有 level 變動；book 年齡因此以「交易所最後一次確認」計，不以「內容最後改變」計。

7c-ii. **Optimizer 評分（`execution/deployment/rate_optimizer.py`，純函式）**：候選為 `signal`（signal rate）與可選的 `maker`／`taker`。maker／taker 帶的 fill evidence 若與 signal evidence scope 不同 → `evidence_scope_mismatch`。合格條件：有 fill evidence、rate 有限且 >0、且 **rate ≥ signal rate**（optimizer 只會往上加價，不會低於訊號）。每個候選只用**以自己價格估出的** evidence 計分：`score = rate × fill_prob × (1 − fee_rate)`（`fee_rate ∈ [0,1]`）。取 score 最大者；平手依序比 `fill_prob` 較高、再比 **rate 較低**（保守）。無合格候選 → `no_eligible_candidate`。`OptimizerNoRecommendation` 刻意與 execution 的 `NoRecommendation` 是不同型別，不可能被誤送單。

7d. **Audit ordering**：每個 allocation candidate 先 append 一筆不可變 `execution_decisions`（`ready`、`blocked` 或 `no_recommendation`）。只有 READY audit commit 成功，才建立 `ReadyToSubmit` 並交給 executor；audit 失敗一律 block。成功送出的 `ReservationIntent` 帶相同 `decision_id`，但 execution audit 不是 ledger projection，也不改變 capital state。executor 的 closed outcome vocabulary 為 `acknowledged`、`rejected`、`unknown`、`not_sent`；只有 `acknowledged` 才 `tracker.record_deploy`，`unknown` 開啟 symbol-level uncertainty gate，`rejected`/`not_sent` 保持 capital-neutral。

**成交與重部署**

8. WS `foc EXECUTED` → `OrderFilled` → reserved 降、realized 升（見 3c）。credit 到期由 Bitfinex 自動觸發，**不需顯式 redeploy**：下一個 90s tick 重算 gap 時，被釋放的額度自然回到分配池。

**為什麼這樣設計（WHY）**

- **rate 慢、idle cost 連續**：策略 rate（EMA / percentile window）算起來「慢」且對 1h 尺度才有意義，但閒置資金每秒都在損失機會成本。把 rate 凍結成 ~65min TTL 的 standing quote，讓資金分配能以 90s 高頻運作而不必每次重算訊號。TTL 確保 rate regime 變動後過期的 quote 不會繼續部署。
- **greedy emptiest-first**：固定70% cell limit，不因只有一個 active cell 而放寬。各 cell 共用 account spendable，不能把各自 max_new_offer 加總當成帳戶權限。
- **per-cell intent 只用 reserved**：venue 的 realized credits 無法回溯歸屬到 cell（venue→cell attribution problem，funding credit 不帶 client cid）。若把 realized 算進 rescale factor，per-cell 意圖會被灌爆超過 `cap_per_cell` 並餓死其他 cell。因此 `CellDeploymentTracker` 只 rescale 到 reserved 總量，並對任何超過 `cap_per_cell` 的 rescale 做 hard clamp（defense-in-depth）。

**參數總覽**

| 參數 | 值 | env |
|---|---|---|
| Live capital policy | fUST all_available；fUSD disabled | DB applied revision/digest；無 YAML/env money authority |
| Live reserve | 0（明確 applied policy） | reserve_amount；不接受 legacy buffer fallback |
| Local inferred min offer | USD150 ÷ current public UST→USD FX，門檻取最小8-decimal合法數；實際送單下限為該門檻 ×1.005（`funding_rules.submit_amount`，`RULE.submit_margin=0.005`：venue 會以自己的匯率再換算一次，剛好卡門檻會擲硬幣）；`validate_amount` 仍以精確門檻驗證；offer金額只向下量化 | adapter versioned rule；FX從request start起≤30000ms；非venue接受保證。舊153／env floor+buffer僅歷史simulation |
| Per-cell concentration | max(0, total_capital - reserve) ×0.70；單一 active cell 仍為70% | applied max_cell_fraction；舊100% relaxation已移除 |
| Standing quote TTL | 3,900,000 ms（~65min） | `BFX_QUOTE_TTL_MS` |
| Reconcile interval | ~90s（resync debounce 10s） | `BFX_RECONCILE_INTERVAL_S`, `BFX_RESYNC_MIN_INTERVAL_S` |
| Period | 2 天（兩策略皆 `period_days=2`） | — |
| Reprice sweep（E1） | enabled（canary 2026-07-07 起）；tolerance 10%；min age 30min；≤3 cancels/tick | `BFX_REPRICE_ENABLED`、`BFX_REPRICE_TOLERANCE_PCT`、`BFX_REPRICE_MIN_AGE_S`、`BFX_REPRICE_MAX_CANCELS_PER_TICK` |
| Execution policy | `book_guarded`、`optimizer_shadow`、`optimizer_live`；必須設定 book freshness/reconcile/down-bound，optimizer_live 另需 empirical artifact + fee | `BFX_EXECUTION_POLICY`、`BFX_BOOK_*`、`BFX_FILL_MODEL_ARTIFACT`、`BFX_OPTIMIZER_FEE_RATE` |

### 4a. 回測引擎契約（`modules/backtest/engine.py::run_backtest`）

- `strategy.observe()` 對**每一根** candle 都呼叫（含 cooldown 與 record window 外），讓累加器 warmup；`decide()` 只在 cooldown 結束且 `mts ∈ [record_start_mts, record_end_mts]` 時呼叫，只有 window 內的交易計入 n_trades／equity／sortino。
- 策略**看到**的序列與**定價**的序列分開：`market_candles`（依 mts 對齊）或 `market_series_by_agg`（依 lock 天期選 `p{period}`，否則退到 `a30` 聚合，兩者皆無則 KeyError；`resolve_market_series_key`）。兩者互斥。決策 mts 沒有 market print → `BacktestIncomplete("market_series_gap")`，絕不退回用觀測 candle 或假設 100% 成交。
- 市場利率只來自 `candle_close`（`market_rate_source`）；FRR 不是可換算的市場利率代理。
- `fill_model="empirical"` 時，fill model 必須存在、有 artifact、source ∈ {`candle`,`book`}、horizon 與 symbol／period_agg 與被定價序列一致，否則以 `fill_model_missing`／`fill_model_low_confidence`／`fill_model_scope_mismatch` 拒跑（preflight 在任何計算前）；不會靜默退化成 linear。`linear-baseline` 為明示的 baseline。

---

## 5. Event Model & Source-of-Truth

`event_log` 是不可變、append-only 的財務 SoT（PK `event_seq` BigInteger auto-increment，無 UPDATE/DELETE）。所有 ledger 狀態都是它的投影；pre-trade eligibility 則另存於同樣 append-only、但不影響 ledger 的 `execution_decisions`。每筆 execution event（含 `VENUE_SNAPSHOT_OBSERVED`）必須經 `AccountEventWriter`：先取得 `(exchange_account_id, deployment_environment)` transaction advisory lock，再依 `projection_heads.last_event_seq` replay 缺口、投影目前事件並在同一 transaction 推進 cursor；任何投影錯誤都 rollback event、snapshot 與 cursor。

**Domain events（frozen dataclass）**：`ReservationIntent`（write-ahead，PENDING，不上 bus）、`ReservationClaimed`（reserved += size）、`OrderFilled`（reserved → realized）、`ReservationReleased`（cancel/expire/missing）、`ReservationFailed`（terminal，capital-neutral，不上 bus）、`ReservationUnknown`（ambiguous submit，pessimistic uncertain exposure）、`VenueOfferQuarantined`（無 provenance 的 active offer，絕不 synthetic CID/自動 cancel）、`VenueSnapshotObserved`（full-account REST truth）、`CancelRequested` / `CancelAcknowledged`（audit-only）、`PositionReconciled`（由 snapshot 導出的 bus signal，絕對覆寫）。每個 event 帶 `occurred_at_ms`（domain 時間）、`recorded_at`（wall-clock）、`event_seq`、`venue_seq`（WS dedup 用）。

**核心表與角色**

| 表 | 角色 |
|---|---|
| `event_log` | SoT，append-only。dedup unique index gate `ORDER_FILL` / `RESERVATION_RELEASED`；`VENUE_OFFER_QUARANTINED` 另以 account/env/venue object 應用層冪等。 |
| `projection_heads` | 每個 account/environment/projector 的單調 high-water mark；記錄 `last_event_seq`、`projector_version` 與更新時間，供缺口 replay 與 account-local lag。 |
| `position_state` | ledger 投影（每 account/env/symbol）：`offered_amount`、`lent_amount`、`available_amount`、`uncertain_amount`、`last_venue_snapshot_at`、`last_event_seq`。`reserved`/`realized` 僅為 staged migration 相容欄位。 |
| `offer_claims` | FSM 快照，PK `(exchange_account_id, deployment_environment, cid)`：state ∈ {PENDING, UNKNOWN, CLAIMED, RELEASED, FAILED}。投影先以原子 `INSERT ... ON CONFLICT DO NOTHING` 讓 DB 仲裁首寫者，再按 account UUID 以 CID／非空 venue offer id／非空 execution decision id 重讀唯一 canonical row。完整 identity 相同才視為冪等並推進 FSM；多筆命中或任何既有 identity 不符皆 fail closed，絕不覆寫 established identity。 |
| `venue_offer_state` / `venue_credit_state` | 以 venue object ID 為 key 的 normalized full-account entity projection；complete snapshot 缺席的舊 active object 轉 `absent` terminal，terminal 狀態單調且不受舊 snapshot 重開。 |
| `reconcile_observation` | 由 `VENUE_SNAPSHOT_OBSERVED` 導出的不可變 checkpoint：每 symbol 的 venue totals + `event_seq_fence` + counts。 |
| `execution_decisions` | 每筆 allocation candidate 的 durable eligibility audit：decision/reconcile/account/realm/cell/symbol/correlation、`ready`/`blocked`/`no_recommendation`、stable reason/dependency、signal/applied rate、amount/period、exact-period book evidence、model evidence、safety/policy、config/service hash 與 timestamps。READY 必須在 submit 前 commit。 |
| `diagnostics` | **非 SoT** forensic 軌跡（DECISION / SAFETY_TRIGGER / CANCEL_AUDIT），best-effort 寫入（失敗不擋交易），prunable 30–90d。 |

**Dedup（雙層）**：`AccountEventWriter` 以 native `event_id`（歷史 v2 則由 immutable row 推導）做 account-scoped identity，再以 venue tuple 作 `ORDER_FILL` / `RESERVATION_RELEASED` domain backstop；quarantine breadcrumb 以 account/env/venue object key 做應用層 dedup。`PostgresEventStore.append()` 及 `EventStorePersister.persist()` 傳播 dedup status。WS dispatcher 對 deduped（重連重送、同 `venue_seq`）的事件 skip publish，維持「persist 失敗就不 publish」不變式。`PaperPositionLedger` 額外有 in-memory `_processed_fills` / `_processed_releases` 防呆。

**Venue hints 邊界**：`execution/ws_dispatcher.py` 與 `execution/fill_tracker.py` 將 WS close／REST disappearance 交給 ledger facade 的 `VenueHintSink`。scope 在 adapter 建構時綁定；port 僅含 venue facts，不含 CID。預設 `execution/legacy_venue_hints.py` 保留上述 persist-before-publish／WS dedup，以及 REST persist 失敗後下次 poll 重試；`CREDIT_CLOSED` 仍僅供 audit。`ledger/wiring.py::build_venue_hint_sink` 建構 dormant ledger adapter，尚未在 apps 啟用：不讀寫 DB，只同步呼叫注入的 `request_resync(reason)` 並發布 `VenueHintNotification`。resync 按 `(kind, venue ID)` 使用本機 monotonic clock 的 **5 秒 leading-edge 固定窗口**；窗口內通知仍逐筆發布。offer 通知以 `venue_offer_id` 為 key；credit close 沒有 offer linkage，只帶 `credit_id`。此步驟不新增 provenance read，因此不補 `attempt_id`；S1-3e 組裝時若已有已驗證 provenance，可再擴充通知。通知永不作為 capital authority。

**Deterministic rebuild**：`rebuild_snapshot_from_log(account_id, environment)` 對含 `VENUE_SNAPSHOT_OBSERVED` 的 stream 從空 projection 依 `event_seq` 重放 immutable event；舊 stream 則沿用 checkpoint ⊕ tail 相容路徑。所有 venue object、position bucket、quarantine/unknown breadcrumb 均可由 event log 重建，不能讀 live API 或當前 projection 值作為輸入。

**Account/環境隔離**：每筆讀寫都帶 `deployment_environment ∈ {prod, shadow, ci}`，所有 money query 以 `(exchange_account_id, deployment_environment)` 複合過濾。legacy `account_id` 不再選擇 request/daemon scope；單一 DB 內可並行跑 shadow/prod/CI 而無 cross-account 或 cross-environment 污染。

**Typed outcome unions**：pre-trade 結果是 `ReadyToSubmit | BlockedExecution | NoRecommendation`（`execution/contracts.py`）；venue submit 結果是封閉的 `SubmitOutcome = SubmitAcknowledged | SubmitRejected | SubmitOutcomeUnknown | SubmitNotSent`（`execution/submit_outcomes.py::classify_submit_response`）。transport 開始前的例外 → `SubmitNotSent`；transport 開始後的任何例外（timeout、network error、cancel）、5xx、缺 response 或無法辨識的 body 一律 `SubmitOutcomeUnknown`；只有明確 2xx 帶 venue offer id 才是 acknowledged、結構化拒絕才是 rejected。`submission_attempts.outcome_kind` 的 CHECK 與此詞彙一致。

**Venue wire layout 單一定義**：Bitfinex funding-offer 陣列（REST `auth/r/funding/offers` 與 WS `fos`/`fon`/`fou`/`foc` 共用）只由 `external/bitfinex/funding_offer_row.py::parse_funding_offer_row` 解析；REST（`auth_rest.py`）與 WS（`auth_ws.py`）都必須由它建構 dataclass，不得各自假設 index（2026-05-26 事故根因即兩處 index 不一致）。

---

## 6. Safety Model

**Guard chain（`SafetyGuardChain.evaluate`）**：依建構順序、由便宜到昂貴執行，**第一個 BLOCK 即短路**。每個 guard 套 2s timeout，timeout 或 exception 一律 **fail-closed**（`allowed=False`）並 emit `safety_trigger`（block → `level=warn`；internal error → `level=critical`）。每次 evaluate 在 `finally` 無條件記 `safety_chain` heartbeat。

評估時機：**deployment-time**——`DeploymentReconciler.deploy()` 對每一筆 fill 在 submit 前呼叫 chain。訊號層不評估 safety（它只寫 quote）。

**L1 hard guards（always-on，順序固定）**

1. `ManualKillGuard`（trading-state guard）— 已觸發但 HALTED 尚未寫入的自動保護、或 durable trading state 不是 `ACTIVE` 即擋新單，跑最前；讀不到 trading state、或從未記錄任何決策，一律視為 HALTED（fail-closed）。撤單不經過它（見下方 Trading state）。
2. `AuthHealthGuard` — executor health 為 `DOWN` 時擋（`DEGRADED` 為 soft warn 不擋）。
3. `HeartbeatGuard` — 只 watch `ws`（market-data liveness，own-loop），`age > threshold_seconds`（live 為 300s，由 `safety.live.yaml` 設定；`threshold_seconds` 為必填參數無 code default）擋。刻意**不** watch `executor`/`safety_chain`（reactive，靜市場時不跳動，誤判會造成 idle restart loop）。
4. `CapitalPolicyGuard` 與 `OfferEnvelopeGuard`：資金上限只來自已套用的 `CapitalPolicy`（沒有 `AllocationCapGuard` / `BuyingPowerGuard`，也沒有 safety config 的 `allocation_cap` / `buying_power` 區段）；鏈尾是 `WriterLockGuard`。

5. `OfferEnvelopeGuard`（`safety/pre_trade.py`，lending envelope ADR 2026-09-25 D1）— 每筆新 offer 對 applied `CapitalPolicy` 的包絡檢查：`max_offer_amount`、天期 [min,max]、同幣別 open 的受管 offer 數（手動單不佔名額）、利率下限 ＝ max(`min_rate_apr`/365, 即時 bid 中位數 × `rate_floor_ratio`)。policy 沒有包絡（schema 1/2）、讀不到、沒有新鮮 book 一律擋。包絡改動走 `scripts/amend_capital_policy.py`（dry run → digest → apply，新 revision）；`enabled` 平常由 UI 請求切換（見下方 Trading state）；撤單不經過它。送單節流 `CommandThrottle` 是平台設定（`pre_trade_limits.command_rate`），不在包絡裡。

**NAV-drop 告警（第 4 級，不擋單）**：`NavDropMonitor` 包住 `ReconcileNavTracker`（per-symbol，NAV＝available＋offered＋lent，原生單位），24h 虧損或 drawdown 超過 `nav_alerts` 門檻時發 `nav_drop` 告警一次。放貸只會少賺、不會讓 NAV 下降；會下降的是提領、轉帳、平台分攤損失，停止放貸補救不了，所以不再有 loss／drawdown guard。

**Trading state（ADR D4）**：`trading_state` 是帳戶層的停機權威，append-only，只有 `ACTIVE`／`HALTED`，cause 為 `operator`｜`auto`；DB trigger 只允許 operator 結束 HALTED，唯一例外是 `HALTED/auto` → `ACTIVE/auto`（見「自動保護」；migration `8e4b2f6a1c37`）（2026-09-25 前的列可能是 REDUCING／`material_deploy`，CHECK 為 NOT VALID 保留原樣，遷移時目前是 REDUCING 的 scope 已補一列 operator HALTED）。日常的單幣別停止是 policy 的 `enabled=false`：不掛新單，reconciler 撤掉該幣別的受管 offer（見下方「受管 offer 的收斂」），已成交借款照常到期。`enabled` 由 webapi 的 TOTP enable/disable 請求切換（`capital_policy_requests`，`CapitalPolicyRequestWorker` 經 `capital_amendment` → `CapitalRepository.apply_policy` 寫新 revision）；停用只看 operator 驗證，不看 build、trading state 或包絡；沒有包絡也可啟用（guard 照擋）。bfx_bot 對 policy 表只有這一種寫法：DB trigger 只准它寫「目前 head 的下一個 revision、除 `enabled` 外完全相同、`source.request_id` 指向一筆仍在等待、同幣別同方向、且 `public.operator_authorized` 仍通過的請求」並把 head 前進一格；偽造的 request id 無法讓 runtime role 自行擴大交易；owner（script）不受限。恢復只經 webapi 的 TOTP resume 請求，不帶任何限額期；static token 只有 `/admin/halt`。部署不寫 trading state；change class 已移除（`deployments.change_class` 由 migration `5b9e3d7a2f41` drop，舊列的 class 仍留在 `detail`）。

**受管與外來 offer（D2）**：有 durable intent 來源（claim／attempt，`venue_offer_state.execution_decision_id`）的才是受管。沒有的是外來（手動掛單、Bitfinex auto-renew）：capital classifier 記入 `foreign`、不算受管曝險（金額本來就不在 venue available 內），bot 不撤不重定價，`foreign_exposure` 告警一次。

**UNKNOWN 與金額指紋（D3a）**：funding submit 沒有 `cid`，planner 把每筆金額末 4 位小數編成同幣別唯一的指紋（`amount_fingerprint.py`，command gate 在 account lock 內再驗一次）。UNKNOWN 只隔離該幣別（第 2 級：capital read 對該 symbol 回 `execution_unknown`），不停機；settle 窗口（120s）後由 recovery 以已存的快照＋history 結案：恰好一筆同指紋、同條件、未被認領的 offer → 認領；history 完整涵蓋且沒有 → 判定未送出；其他 → 維持隔離，30 分鐘後 `unknown_quarantine_aged` 告警，之後每 6 小時一次。

**自動保護（`safety/protection.py`，第 3 級）**：只有「bot 對自己的帳認知錯了」才寫 `HALTED/auto`：`unclassifiable_commitment`、`offer_amount_mismatch`、`identity_conflict`（classifier 的 provenance／attempt／evidence 衝突）、`venue_lent_above_ledger`、`command_rate_exceeded`（只數被拒的 submit；被拒的 cancel 等下一個 token，從不累計成停機）。觸發是同步記錄，guard 立即擋新單；受監督的 task 在鎖外寫 HALTED，就這樣，不碰 venue。自動 HALTED 在條件消失後自動解除（ADR 2026-09-26 auto-halt-resumes-when-condition-clears）：停滿 15 分鐘，且停機後連續 3 個被接受、沒有任何 trip 的 snapshot（`_protect` 回報 `observe_clean`；任何 trip 清空計數），同一個 task 寫 `ACTIVE/auto`（actor `auto-resume`，reason 帶 snapshot 序號）。DB trigger 以 DB 時鐘檢查 15 分鐘與「24 小時內最多 2 次」，超過就維持停機並發 `auto_resume_limit_reached`。operator 的 HALTED 一律寫新列蓋過 `HALTED/auto`（`restates()`），永不自動解除。classifier 衝突類在條件持續時每輪都會再觸發、snapshot 不被接受，所以維持停機；`venue_lent_above_ledger`（比相鄰 snapshot 的變化量，觸發後基準重設）與 `command_rate_exceeded`（停機時不送單）停機後無法重新觀測，實質是「未再發生」即恢復，靠 24 小時 2 次上限擋住反覆發生的 bug（ADR D5）。「借出額高於帳本」只判斷被接受的 snapshot：lent 可以因借款結束而減少、因任何 offer（含外來）縮小而增加；剩下的部分先扣掉 offer history 裡「上次被接受的 snapshot 之後結束的外來 offer 已成交量」，扣完仍 >0.01 才觸發，否則只發 `foreign_lending` 告警。ledger authority 下改在 acceptance 以 venue id 對帳（`ledger/conservation.py`、`_internal/conservation_facts.py`，取代上述 offered 差額的推論，兩者不逐案等價）：新 basis 的每個 symbol 對上一個 accepted basis——新增借出 = 各筆借出（依 period＋opening，沒有則依 id；venue 把一筆 loan 換成同額 credits 不算新增）的增加量總和，借款結束／縮小不是異常也不抵銷增加；區間內開了又關的借出（不在上一個也不在這次的 active，但在這次 observation 的 credit history 且開始於上一個 query 之後）也計入新增借出，loan 與 credit 同樣算借出；成交 = 每個 offer id 在區間內成交量（上一個 observation 的剩餘，新 offer 則為原始額，減去這次的剩餘或 terminal history 的結束剩餘），並以這次 observation 的 funding trades 交叉檢查，兩者不一致，一律 fail-closed。offer 消失（P 在簿上、這次不在 active）且 history 沒有紀錄時：adapter 依 id 向 venue 點查（`{"id":[…]}`，每個 cycle 重試；offers 的 windowed history 依 MTS_UPDATE 分頁，兩者是兩份證據）。查得到 terminal 列就照常對帳。沒有 terminal 答案的原因不論是 `not_returned`、`request_failed`、`cap_exhausted`（共用 request budget 用完）或 `unparseable`，都走同一條路：120 秒 grace 內該 symbol 的 offer history 維持 incomplete（整個 observation 不被接受），grace 從本 process 第一次看到該 id 沒有答案起算，重啟重新計時；grace 過後該 id 不再是 incomplete，observation 帶 `unconfirmed_ends`（寫進 observation 的 evidence，basis 從 DB 讀回，同 `history_symbols`；不產生 terminal 列，mirror 不寫 terminal，缺席仍不證明 terminal），並發 `offer_end_unconfirmed` 告警（帶原因）。conservation 只以這次 observation 中該 OFFER_ID 的 funding trades 定它的成交量，且必須精確：trades 涵蓋 P 的起點、P 讀取前後的時鐘容忍帶內沒有 ambiguous trade（possible = certain）、成交量不超過 P 剩餘額；單純撤單沒有 trade，成交為 0。定得出來就照常算（可為 `conserved`），定不出來或不一致仍是 conflict → `unexplained_lending`。判決寫完後再發 `offer_end_judged`（explained／undeterminable、offer id、symbol、成交量與該 symbol 的 verdict），「trades 無法定案」的重評觸發量的就是它。Coverage 有兩種 offer 證據：windowed query（`history_requested_*`、`offer_history_pages`、`history_oldest/newest_mts_created` 只描述它；UNKNOWN matcher 與 R6 只讀這些），以及消失 id 的點查（只對該 id 是 terminal 證據，缺席不是證據，不擴大範圍、不計頁數）；`offer_history_complete` = windowed 抓到底，且每個消失 id 不是查到就是列入 `unconfirmed_ends`。`raw` 由 adapter 產生 JSON-safe 值（精確小數以 `format(v, "f")` 字串保存），ledger 的 `_validate` 拒絕任何非 JSON 值（含 Decimal），不轉換。`unexplained = 新增借出 − 成交`，|unexplained| > 0.01（任一方向）或有不一致即 `unexplained_lending`；否則外來 offer 成交 > 0.01 為 `foreign_lending`（只告警），其餘 `conserved`，首次或沒有前一筆則 `baseline`。判決連同 `lent_unexplained`（有號）、`foreign_executed`、`fill_conflicts` 存在 `accepted_capital_basis_symbol`，不在記憶體跨 snapshot 攜帶；`unexplained_lending` 讓該 symbol 的 capital read 回 `Blocked(venue_lent_above_ledger)`（對應 `VENUE_LENT_ABOVE_LEDGER` trigger），下一個 accepted basis 以它為新基準。writer lock 遺失不是交易決策：`WriterLockWatch` 拋 `WriterLockLostError` 讓 daemon 退出、container 重啟。拒絕開機（schema 不符、policy 讀不到）什麼都不寫、不碰 venue，只發 `venue_offers_may_remain` critical 告警後退出：停機狀態會比原因活得久，修好的下一版不該還要 resume。

**受管 offer 的收斂（`DeploymentReconciler._pull_if_stopped`）**：唯一撤受管 offer 的地方，level-triggered。每個 tick、每個幣別：帳戶 `HALTED`（任何 cause）或 policy `enabled=false` 時，這個幣別不規劃任何新單，並經 command gate 按 venue id 撤掉仍開著的受管 offer（`managed_cancel.ManagedOfferSweep`，`execution_decision_id` 不為空者）；某次撤單被拒或失敗，下一個 tick 再撤。外來 offer 與已成交借款從不碰。

**撤單資格**：`AccountCommandGate.cancel` 走 `SafetyGuardChain.evaluate_cancel`，只跳過 `capital_policy` 與 trading-state guard；受管 provenance、同 scope 的新 UNKNOWN／讀取失敗仍在 admission 與每次 transport 前拒絕撤單。已保留 intent 的 submit 在 transport 前走 `evaluate_transport`，仍受 trading state 約束。

**Kill switch（`safety/kill_switch.py`）**：operator 專用（UI kill 請求、`POST /admin/halt`），是唯一的 venue cancel-all。先 commit `HALTED`（寫不進去就不呼叫 venue），再對每個幣別呼叫 `POST /v2/auth/w/funding/offer/cancel/all`，**連手動掛的 offer 一起撤**；幣別＝設定的 symbols，加上有 open uncertainty 或非終態 venue offer 的 symbols；只需 writer lock，不經 command gate；每次呼叫在 `funding_cancel_all_audit` 留 `requested` 與一筆終態；venue 失敗不回滾 HALTED，再 kill 一次即重試（`/admin/halt` 在未全數完成時回 502）。不依賴資料庫的 break-glass 是停掉 bot container。

**真錢 guard 不變式（`assert_live_guard_invariant`）**：`BFX_PHASE=live` 啟動時強制 trading-state/auth/heartbeat hard guards 全開，且必須有 `pre_trade_limits.command_rate`；每幣別的包絡在 DB policy，缺包絡的幣別由 `OfferEnvelopeGuard` 擋單。

**Config 不可熱載**：`SafetyConfig` 啟動時讀一次（`BFX_SAFETY_CONFIG`，預設 `configs/safety.live.yaml`），改 threshold 需 redeploy。

**/healthz liveness**：獨立 HTTP server（port 8080）。Liveness（own-loop，如 `ws`）staleness 致命 → daemon 自行退出，由 compose 的 `restart: unless-stopped` 重啟；Activity-class（reactive，如 `executor`/`safety_chain`）只 WARN 不致命——這是 2026-05-26 idle-market restart loop 修法的核心。`bfx-deploy` 部署時以 `/healthz` 做 health wait（bot 300 s）。現行 `deploy/vm/docker-compose.app.yml` 沒有 Docker healthcheck，也沒有 autoheal label；`willfarrell/autoheal` sidecar 只存在於 `docker-compose.bot.yml` 的 `legacy-app` profile（歷史定義，不可用來啟動應用程式），因此 restart 只來自 process 退出，不來自外部探活。

**`/readyz` trading readiness**：`/healthz` 只回答 process liveness；`/readyz` 回傳 `200 {"trading_ready": true, "reason": null}` 或 `503 {"trading_ready": false, "reason": <stable reason>}`。book、model 或 audit dependency 不可用會將 `trading_ready=0`，供 operator status/metrics 告警，但不會令 liveness restart loop 啟動。

**Execution observability**：固定 structured event names 為 `funding.execution.eligibility`、`funding.execution.blocked`、`funding.execution.submitted`、`funding.book.snapshot_invalid`、`funding.fill_model.unavailable` 與 `funding.optimizer.no_recommendation`。每筆帶 decision/reconcile id、policy、outcome、reason 與 bounded evidence；不含 secret 或未界定 exception text。Prometheus 使用 `bfx_execution_decisions_total{outcome,reason,policy}`、`bfx_execution_audit_persist_failures_total`、`bfx_funding_book_snapshots_total{result}`、`bfx_funding_book_snapshot_age_seconds`、`bfx_execution_gate_duration_seconds`、`bfx_trading_ready`。cell、symbol、candidate rate、artifact hash 與完整 book evidence 僅留在 logs/audit，絕不成為 metric label。

**Telemetry 與 diagnostics 分流**：operational telemetry（SIGNAL／HEALTH_CHECK／ORDER_SUBMIT／lifecycle 與上述 execution events）只寫 stdout 的 JSON line（`observability/stdout_sink.py`，logger `bfx_funding_bot.events`），由容器 log driver 收集，沒有第三方 log 平台 client；metrics 走 Prometheus，OTLP tracing（`observability/tracing.py`）預設關閉且 fail-open。forensic 的 DECISION／SAFETY_TRIGGER／CANCEL_AUDIT 由 `DiagnosticsSink`（`execution/diagnostics/sink.py`）寫 `diagnostics` 表：每筆**自己開一個 transaction**（`session_scope`，絕不搭 command transaction），失敗只記 log、永不往上拋，未列在 `_KIND_BY_EVENT_TYPE` 的事件直接丟棄。

**Writer lock（`core/writer_lock.py`）**：兩個 venue 的 daemon 開機時對 Postgres 取 session-level advisory lock，key 為 `blake2b("bfx-writer:{exchange_account_id}:{env}")` 的 signed int64（不用 process-salted `hash()`）；被別人持有 → `WriterLockUnacquired` → exit code 75（`EXIT_CODE_WRITER_LOCKED`）。`AccountEventWriter` 的 per-transaction advisory lock 用不同 namespace（`bfx-writer-xact`），兩者不互等。每筆真錢 submit 前 `WriterLockGuard.verify_held()` fail closed；`_writer_lock_liveness_loop` 每 30 s 以 `WriterLockWatch.check()` refresh（連線斷掉時重取），refresh 後仍未持有 → `WriterLockLostError`，daemon 退出、container 重啟、開機再等鎖；不寫 trading state。

**Heartbeat：liveness vs activity（`marketfeed/health_monitor.py`）**：每個 sub-task 只能屬於其一（import 時 assert 不重疊）。`LIVENESS_THRESHOLDS`（own-loop，會自己週期跳動：`ws` 90 s、`candle_writer`／`scheduler` 65 min、`health_check` 6 min、`db_keepalive` 7 min、`fill_tracker` 90 s、`periodic_reconcile` 270 s）過期 → `FatalError` → TaskGroup 結束 → process 退出、由 container restart policy 重啟。`ACTIVITY_THRESHOLDS`（reactive，靜市場時合法不跳：`safety_chain`、`executor`、`writer_lock`）過期只發 degraded／down 觀測事件，永不致命——把 reactive task 接到 liveness 就是 2026-05-26 idle-market restart loop 的成因。auth WS 健康同樣只觀測、不驅動 restart。

---

## 7. DB Schema（key tables）

```
event_log              (SoT, append-only)
  PK event_seq
  exchange_account_id (UUID NOT NULL FK RESTRICT), legacy account_id (audit only),
  deployment_environment, event_type, cid,
  venue_offer_id, venue_seq, payload(JSONB),
  occurred_at_ms, recorded_at
  UNIQUE dedup(exchange_account_id, deployment_environment,
               event_type, venue_offer_id, venue_seq)

position_state         (ledger 投影, per account+env+symbol)
  PK (exchange_account_id, deployment_environment, symbol)
  offered_amount, lent_amount, available_amount, uncertain_amount,
  reserved/realized (staged compatibility aliases),
  last_updated_ms, last_event_seq,
  last_reconciled_at, n_credits

offer_claims           (FSM 快照, per-offer)
  PK (exchange_account_id, deployment_environment, cid)
  state{PENDING|UNKNOWN|CLAIMED|RELEASED|FAILED}, venue_offer_id,
  size_usdt, signal_correlation_id, execution_decision_id,
  occurred_at_ms, last_updated_ms, last_event_seq
  UNIQUE partial (exchange_account_id, deployment_environment, venue_offer_id)
    WHERE venue_offer_id IS NOT NULL
  UNIQUE partial (exchange_account_id, deployment_environment, execution_decision_id)
    WHERE execution_decision_id IS NOT NULL

reconcile_observation  (checkpoint, append-only audit)
  PK id
  exchange_account_id, legacy account_id (audit only), deployment_environment,
  reserved_usdt, realized_usdt, n_offers, n_credits,
  observed_at_ms, event_seq_fence, recorded_at

venue_offer_state / venue_credit_state (normalized venue object projections)
  PK (exchange_account_id, deployment_environment, venue object id)
  terminal state is monotonic; complete observations close absent objects;
  partial coverage preserves the prior bucket rather than writing a false zero

execution_decisions    (append-only pre-trade audit；非 ledger projection)
  decision_id, reconcile_id, exchange_account_id, deployment_environment,
  outcome, reason_code, failed_dependency, signal_rate, applied_rate,
  amount_usdt, period_days, market_snapshot_evidence, model_evidence,
  safety_result, execution_policy, service_version, config_hash,
  occurred_at_ms, recorded_at

trading_state          (append-only 交易狀態；帳戶層停機的唯一權威)
  PK id（insert trigger 在 scope lock 下指派，id 序即決策序）
  exchange_account_id (FK RESTRICT), deployment_environment,
  state{ACTIVE|HALTED}, cause{operator|auto}, actor, reason, created_at_ms, legacy_halt_id
  -- state/cause CHECK 為 NOT VALID：5b1e7c9d2a40 之前的列保留原本的 REDUCING／material_deploy。
  -- trigger 只准 operator 結束 HALTED，並拒絕 UPDATE/DELETE/TRUNCATE；bfx_bot SELECT/INSERT，bfx_webapi 只有 SELECT。
  -- legacy_halt_id 指向 release_archive.trading_halt 的來源列（無 FK）。

release_archive.*     (已退役 release ceremony 的真錢紀錄；migration c74d45a54e46)
  trading_halt, canary_command_permits, release_sessions, release_session_audit（c74d45a54e46）;
  deployment_approvals, trading_control_requests_v1, trading_state_probation（5b1e7c9d2a40）
  -- 原表連同欄位、約束、索引與指向 public 的 FK 整張移入（SET SCHEMA），statement-level
  -- trigger 拒絕任何寫入；manifest 記每表列數與依 PK 排序的 to_jsonb 內容 SHA-256，可重算驗證。
  -- 任何 app role 都沒有 schema USAGE；DB owner 直接以 SQL 查詢；downgrade 原樣搬回 public。

trading_control_requests (webapi→daemon 請求；webapi 只 INSERT 請求欄位)
  request_id, action{resume|kill}, reason, requested_by,
  created_at_ms, state{requested|applied|rejected|failed}, processed_at_ms,
  outcome_reason, trading_state_id
  -- 每個 scope 至多一筆 pending（kill 另有自己的一格）；請求欄位不可改，state 只能從 requested 轉一次終態。

capital_policy_requests (webapi→daemon 幣別啟停請求；migration 7d2a9c4e6b13)
  request_id, exchange_account_id, deployment_environment, symbol, action{enable|disable},
  reason, requested_by, created_at_ms, state{requested|applied|rejected|failed},
  processed_at_ms, outcome_reason, policy_revision_id (FK capital_policy_revisions)
  -- 同一幣別同一動作至多一筆 pending；applied 必帶當下生效的 revision（unchanged 時為原 revision）。
  -- 與 trading_control_requests 分表：kill 不與它共用佇列或 pending 格；kill 套用時把等待中的 enable 標
  -- superseded_by_kill，disable 照常套用。

capital_policy_revisions / capital_policy_heads (append-only 版本化 CapitalPolicy；head 是唯一可變指標)
  -- bfx_bot：revisions SELECT/INSERT、heads SELECT + UPDATE(revision_id, revision)，trigger
  -- guard_runtime_policy_revision／guard_runtime_policy_head 把非 owner 的寫入限縮成只切 enabled；
  -- bfx_webapi 只有 SELECT（overview 列出 policy 與包絡）。

funding_cancel_all_audit (append-only；kill switch 每次 venue cancel-all 的紀錄)
  PK id, exchange_account_id, deployment_environment, trading_state_id (FK),
  attempt_id, currency, phase{requested|acknowledged|rejected|failed|skipped},
  venue_status, detail, actor, occurred_at_ms
  -- 每個 attempt 一筆 requested、至多一筆終態（partial unique）。

diagnostics            (非 SoT forensic, prunable)
  exchange_account_id, deployment_environment, kind, payload(JSONB),
  occurred_at(TIMESTAMPTZ), recorded_at

funding_candles        (訊號層輸入)
  PK (symbol, timeframe, period_agg, mts)
  is_final, first_seen_at_ms, finalized_at_ms
  -- is_final=false 的列仍在成形中（venue 會用同一個 mts 反覆推送不同 close）。
  -- 只有 is_final=true 可餵策略；upsert 對已定稿列是 no-op（見 I-CI）。

funding_candle_revisions (append-only，定稿後被拒寫入的證據)
  PK (id)；索引 (symbol, timeframe, period_agg, mts)
  observed_at_ms(knowledge time), rejected_close, final_close
  -- 用來回答「venue 到底會不會修訂已收盤 bar」。長期為空 = bitemporal 層可降級。

config_regime          (非 SoT telemetry, prunable, 每次 daemon boot 一筆)
  PK (deployment_environment, exchange_account_id, recorded_at_ms)
  clamp_enabled (legacy telemetry; always false), reprice_enabled,
  git_sha
  用途：flag flip 需重啟（config boot-immutable），boot 即為 regime
  boundary；供 execution-quality（fill latency、realized APR）歸因對應
  的 flag regime，不必等 weekly window 累積。

exchange_accounts      (money-domain aggregate root；accounts/tables.py)
  PK id (UUID, immutable；ORM 拒絕改寫), venue, label,
  lifecycle_status{active|halted|retired}, created_at, retired_at
exchange_account_memberships
  PK (exchange_account_id FK RESTRICT, user_id), role
exchange_account_credentials (AEAD 加密；AAD = canonical account UUID)
  PK id (UUID), exchange_account_id FK RESTRICT, venue, api_key,
  secret_ciphertext/nonce, wrapped_dek/dek_nonce, key_version, lifecycle_status
  UNIQUE partial (exchange_account_id, venue) WHERE lifecycle_status = 'active'

submission_attempts    (每次 venue submit 一列；uncertainty_tables.py)
  PK attempt_id (UUID)
  execution_decision_id UNIQUE FK execution_decisions, exchange_account_id FK,
  deployment_environment, symbol, cid, normalized_payload, payload_sha256,
  outcome_kind{acknowledged|rejected|unknown|not_sent|NULL=未完成}, venue_offer_id
execution_uncertainties (未解的 venue 證據造成的 account/symbol 阻擋)
  PK uncertainty_id (UUID)
  UNIQUE (exchange_account_id, deployment_environment, symbol, kind, correlation_key)
  UNIQUE partial (exchange_account_id, deployment_environment, symbol, kind)
    WHERE state = 'open'

funding_stats          (Bitfinex /v2/funding/stats/{symbol}/hist)
  PK (symbol, mts)；symbol 一律帶 `f` 前綴（`fUSD`/`fUST`）
  -- endpoint 實測上限 limit=250（≥500 回 HTTP 500，官方文件的 10000 是
  -- candles 的上限）；client 與 backfill 預設 page 250，回傳 < page 即到底。
fill_rate_stats        (fill-rate 學習結果；lending/tracking/tables.py)
  PK (source, symbol, period_agg, horizon_h, spread_bucket_bps)
  artifact_hash FK fill_rate_model_artifacts（source 不同的列不混用）
perp_funding_rates     (外部訊號；external_signals/tables.py)
  PK (venue, symbol, mts)
liquidations           (外部訊號)
  PK (venue, pos_id, mts, is_match, is_market_sold)

projection_audit.runs / rows / receipts (projection cutover 的不可變 archive；
                        migration f8c2d4e6a901，不在 runtime create_all)
  -- runs 必須以 complete=false 建立，只允許一次「→ complete」更新，且須在同一
  -- transaction 完成（deferred constraint trigger `complete_at_commit`）；rows 只能在
  -- run 未完成時 INSERT，receipts 只能在完成後 INSERT；任何 UPDATE/DELETE/TRUNCATE
  -- 皆由 trigger 拒絕；schema/table/function 對 PUBLIC REVOKE ALL。
  -- `core/schema_head.py` 把它列為跨 migration 契約（ARCHIVE_CONTRACT_BASE）。
```

**Alembic**：遷移在 `backend/alembic/versions/`。Halt 1 先套用 additive
revision `8a1b2c3d4e5f`，完成 `cutover_identity.py --dry-run/--apply/--verify`
後才套用 contract revision `9b2c3d4e5f6a`。一般部署仍使用
`cd backend && uv run alembic upgrade head`；`alembic/env.py` 在套用前設
`lock_timeout=5s`、`statement_timeout=60s`，並以 `pg_try_advisory_lock` 序列化 migration
（已有另一個 migration 持鎖就立刻失敗，不排隊）；驗證無 drift 使用
`uv run alembic check`；serialized projector 的 `c2e3f4a5b6c7` 會先為既有
event history seed cursor，writer 在此 revision 前 fail closed，避免 legacy
snapshot 被 replay double-count。contract revision 會在 DDL 前拒絕 NULL UUID、未映射
realm、孤兒 FK 或非零 legacy scaffold，且為 forward-only（rollback 使用
verified backup/PITR + venue reconcile，不使用 downgrade）。

### Projection audit cutover release boundary

Archive 驗證、證據格式與再次執行限制見 [projection archive 契約](../docs/runbooks/projection-audit-cutover.md)。
已完成的 cutover 流程為：
diagnose → immutable archive → independently verified archive-only restore → fresh
snapshot → atomic apply → identical repeat → independent new baseline → archive+active verify。
`cutover_projection` 只有 `diagnose/prepare/verify-archive/apply` commands；沒有
classify、snapshot、cleanup 或 resume command。Prepare CLI 會讀 vault/venue，即使
`--dry-run` 亦然；apply 只消費 digest-pinned serialized snapshot，不做 HTTP。
每次 recovery 以 `--managed-symbols` 明確宣告 scope；已完成的 production cutover 使用
`--managed-symbols fUST`，prepared artifact 會 pin 該 scope。snapshot 必須完整覆蓋
scope；任何 scope 外的 active offer/credit/position 都 fail closed，不能把遺漏的幣別
默認當成零。CLI recovery default 為 fUST；library `apply_cutover` 未指定 scope 時僅
維持既有 fUST/fUSD compatibility default，production operator 仍必須明確傳入 scope。
scope 外若只剩所有 exposure/ledger buckets 與 `n_credits` 都為零的 supported-symbol
legacy scaffold，才可由同一個 atomic rebuild 清掉；任何非零值仍 fail closed。

Diagnostic/classification 使用 v2 local evidence directories（`manifest/COMPLETE/chunks`），
directories/files 為 `0700`/`0600`，拒絕 symlink、extra/incomplete content。固定 bounds：
record 1 MiB、part 4 MiB、artifact 2 GiB、4096 parts、manifest 16 MiB。
`EvidenceWriter` 串流寫入，`verify_cutover_evidence` 驗 identity/digests 與逐筆分類完整性，
runtime 只接收 compact `VerifiedCutoverEvidence`。Prepared-v1 小 envelope 維持 kind 與
archive codec，但現在 required `managed_symbols` 會 pin operator scope；舊 envelope
可供 archive verifier 讀取，不能直接作為新的 apply authority，也不能冒充新 scope。
不接受 v1 diagnostic 作為 cutover authority。Local artifact cleanup
只作用於本次產生的 exact paths，需 deadline/absence evidence，不清 immutable DB archive。

Apply 要求 caller-owned READ COMMITTED transaction；先取 account advisory lock，
再查 completed receipt，並以 SHARE ROW EXCLUSIVE table locks fence direct writers。
在同一 transaction 完成 snapshot append、genesis rebuild、event prefix/new head、
fresh DB parity、舊 archive preservation、applied archive 與 receipt。CLI 將 bounded
外部 file/evidence 驗證置於 engine/lock 前，lock_timeout=2s、statement_timeout=30s；
不宣稱整個 apply 都是 constant memory 或具有同等 global deadline。
Before-commit failure 撤銷本次 DB row writes；sequence gaps 與先前 committed prepare
archive 可保留。After-commit output failure 不撤銷 DB；相同 request 的 repeat 驗證
receipt/current state 後回原結果，可接受已過期的原 snapshot，不 append 第二次。
新 apply 的 snapshot 則必須完整且自 query start 起 ≤300 秒，包含執行耗時。

實際 runtime roles（含 reachable roles/PUBLIC/column grants/ownership）必須證明無
archive 寫入能力；superuser 是未解 operator gate。Persistent halt、controlled
Docker/systemd writers 與 DB session inventory 必須同時成立，不能將 lock 當成永久
禁止外部 admin 啟動 writer。歷史 CID cycles 的 genesis replay 不等於 strict append
可補齊落後 head；這個 source-specific seam 必須在 private rehearsal 驗證，失敗保留
halt，不改 event/head 來繞過。Production schema/grants/archive/apply 各須 explicit
operator approval；runtime identity/KEK/auth denial、fresh exposure、no uncertainty、
RPO/RTO、two fresh reconciles 仍是另外的 gates。

### Offsite DR source of truth

PostgreSQL WAL archive 與 base backup 以 **pgBackRest** 寫入 private Cloudflare
R2 repository；tracked config 不含 endpoint、bucket、credential 或 cipher
passphrase。`status.sh`、`preflight.sh` 與 isolated restore drill 只產生 bounded、
redacted、`measured: true|false` evidence，供營運者與 freshness 告警核對 RPO、RTO、
event head/hash 與 empty-projector replay，而不是用設定存在或檔案存在推定可恢復。
DR 狀態不擋交易或 resume（ADR 2026-09-25 D6）。

Isolated restore 的 staged boundary 固定如下：Compose 只以 `restore-data` 作為
logical volume key，由 runner 注入 generated external volume name；production
`bfx_pgdata` 永不成為 restore target。`restore-db` 先同時加入 generated internal
network 與 temporary R2 egress network，healthcheck 後須以 SQL role `bfx` 在既有
baseline DB 確認 `pg_is_in_recovery()=false`（同一 global deadline），才斷開
R2 egress，核對 membership 恰好只含 generated internal network。
runner 透過 local socket 以 OS user `postgres`、SQL admin role `bfx`
連入 existing restored database，建立 ephemeral verifier role；verifier 不取得
R2 credential、application secret 或 Bitfinex connectivity。所有 container-side
pgBackRest/psql command 都使用 `--user postgres`，production PostgreSQL 則以
`archive_timeout=60s` 確保低寫入量時仍有 bounded WAL archive latency。

Stanza 固定 `pg1-user=bfx`；health/recovery/bootstrap/schema/capture 同用 SQL
`bfx`，不靠 DR `POSTGRES_*` initialization。所有 Compose subprocess 使用 sanitized
env，移除 ambient `DR_*`、`DATABASE_URL`、`POSTGRES_*`、`BFX_*`、`COMPOSE_*`，
保留 PATH／Docker transport。Verifier container 使用 generated name，啟動 attempt
前標記 cleanup eligibility；timeout 後仍須独立清理，先於 networks，整體 cleanup
仍受獨立 30 秒 deadline 約束。

pgBackRest 2.59.1 info 的 `timestamp.start/stop` 是 strict integer epoch；
stanza/repository `status.code` 必須為 integer 0。Smoke raw stdout/stderr 只進
trap-cleaned private temp，輸出固定 markers。Backup refresh 先失效舊 evidence；
atomic persistence 遇 ENOSPC 亦須 remove/truncate 舊綠燈，wrapper 失敗不得 cat 舊報告。
Canonical UUID baseline/staged replay 要求已驗證的 account identity 與相符 schema；
pre-identity 的 legacy-schema-compatible backup/restore evidence 不可沿用。
Baseline 操作見 [offsite DR](../docs/runbooks/offsite-dr.md#6-capture-the-same-target-bounded-baselinejson)。

每次 measured restore 都綁定同一 backup/PITR target 的 bounded baseline，逐欄
核對 migration heads、event count/head/hash 與 account/environment/projector。
Completed projection archives 要求 schema-v2 baseline 的完整 manifest/prepared-file
references 與 verifier image pin。`restore-drill.sh --archive-only --target-run-id`
使用 v2 archive transport，驗 target 與舊 prefixes/bytes，略過舊 active parity，產生
`kind=archive_restore`；它只解除 prepare 與舊 parity 的依賴，不放寬 full DR。
Apply 後以 source 獨立建立新 baseline、配對新 backup/PITR，complete restore 不帶這兩個
flags，使用無 target 的 v1 transport 驗所有舊/applied archives 與 active replay。
不得從待驗 restored DB 或 apply receipt 倒算 expected event baseline。
backup/restore evidence 必須有 strict `observed_at_ms`，讀取時不得在未來且不得
超過 900 seconds；missing、stale、baseline mismatch、egress 未斷開或 cleanup
failure 一律是 `measured: false`，舊 green report 不得沿用。只有完成真實 R2
stanza/check/full/diff/info/verify、disposable expire 與 staged isolated restore 的
新鮮量測，才能宣告 DR ready；offline green 不代表 R2、systemd 或 production
acceptance。

Isolated restore 只量測 generated resources 上的 data integrity，不會還原
production volume，也不是 **venue rollback**。任何 restore point 之後可能發生
的 venue mutation 仍須保持 halt、fresh full-account reconcile，再依
[offsite DR operator runbook](../docs/runbooks/offsite-dr.md) 與
[venue-write rollback runbook](../docs/runbooks/rollback-after-venue-write.md)
處理 adopt、manual resolution 與 forward-fix。

---

## 8. Phases & Deployment

**現行部署 phase（`shadow`／`live`）**

| Phase | 性質 | Realm | Venue | 備註 |
|---|---|---|---|---|
| `shadow` | 模擬校準 | `shadow` | simulated | 跑在 in-process 模擬 venue 上，只收 `ledger` authority，詳見下節「Venue 軸」；`paper` phase 與 echo executor 已刪除，`BFX_PHASE=paper` 在 `load_config` 直接拒絕並指向 `shadow` |
| `live` | **真錢能力** | `prod` | Bitfinex | `live.env`／`book_guarded`；資金只由已套用 CapitalPolicy 決定：fUST all_available、reserve0、max_cell_fraction0.70，fUSD disabled；能否掛新單看 trading state（ACTIVE）、該幣別 policy `enabled` 與包絡 guard（§6）；release flow／change class 已移除，部署不影響能否交易 |

**Venue 軸（`core/venue.py`、`apps/venue.py`）**：`Venue = "bitfinex" | "simulated"` 是 `MarketfeedConfig.phase` 的計算屬性（`live→bitfinex`、`shadow→simulated`），不儲存也不能單獨設定。`BFX_ALLOCATION_CAP_USDT`、`BFX_BALANCE_BUFFER_USDT`、`BFX_CONCENTRATION_PCT`、`BFX_VENUE_FLOOR_USD`、`BFX_MIN_OFFER_BUFFER_PCT`、`BFX_EXECUTOR` 任何 phase 設了都在 `load_config` 拒絕（資金上限是已套用 CapitalPolicy 的，venue 由 phase 決定）。兩個 venue 的開機順序相同：schema head → `database_realm` → authority（該 venue 的集合）→ 之後才碰 vault、憑證與任何 client。simulated 的任何拒絕都 dispose engine 並擲出（不寫、不碰 venue）；Bitfinex 路徑另走 `_refuse_live_boot` 告警。

`build_venue(config, ...)`（`apps/venue.py`，唯一 import `simulated_venue.wiring` 的地方，也是唯一決定 venue 差異的地方）對兩個 venue 回傳同形的 `VenueWiring`：憑證（來源也在此：Bitfinex 讀 vault，simulated 每次開機以 `secrets.token_hex` 產生，且 `BFX_VAULT_KEK` 出現在環境中就拒絕開機）、auth REST 與 executor（含 kill switch 的 cancel-all）使用的 `httpx` client、每個 process 一個 `AuthRequestGate`、`VenueCapabilities`（`auth_ws` required／forbidden、是否允許 REST fill tracker；`build_executor` 檢查 capabilities，不比對 venue 字串）、venue 自己的 task 與 `aclose`（`Daemon.venue_tasks`／`venue_aclose`，開機時先啟動並等 ready）。`load_account_bootstrap` 只讀帳戶列與 config draft，憑證由 wiring 提供。架構測試禁止 `apps/venue.py` 以外的程式比對 venue 名稱。
- **bitfinex**：process 共用的真 client，requires auth WS（`BFX_WS_CLIENT_ENABLED=true`）。
- **simulated**：`SqlVenueEventStore.open(engine)` 開在同一個資料庫（log 是 `sim_venue_event`），realm 必須是 `shadow`／`ci` 且 epoch 為 `ledger`；auth REST／executor 走 `httpx.AsyncClient(transport=<venue>)`，因此不會有任何 authenticated request 離開 process；公開 client 仍是真的（`FundingRules`、蠟燭、book）。沒有 auth WS 也沒有 REST fill tracker（`BFX_WS_CLIENT_ENABLED`／`BFX_FILL_TRACKER_ENABLED` 必須關閉，否則 `build_executor` 拒絕）；漏掉的 fill 靠週期 reconcile 與 resync 補上。`BFX_SIM_INITIAL_WALLETS=UST:1000,...` 只在 venue log 為空時以一次 append（`expected_seq=0`）入帳，重啟不重複入帳，賽跑輸家得 `ConcurrentAppendError`；Bitfinex wiring 設了它就拒絕。
- **兩者相同**：同一條 safety config（`configs/safety.live.yaml`）、同一條 guard chain（含 `CapitalPolicyGuard`、pre-trade guards、`WriterLockGuard`）、reconciler、`InterestLedgerSync`／`CreditHistorySync`、kill switch（`venue=executor`）；每個 symbol 都必須讀得到已套用的 ledger CapitalPolicy。模擬組裝不建立 `PaperPositionLedger`、`OfferRegistry`、`EventStorePersister`、任何 `Legacy*`。兩個 venue 的開機拒絕都走 `_refuse_live_boot` 告警（告警路由是設定，`AlertSink` 已加 realm 前綴）。

模擬 venue 的市場資料（`modules/simulated_venue/live_feed.py`，模組不 import `external`）：venue 在自己的 request lock 內讀 feed，所以 `book()`／`trades()`／`complete_through()` 只讀記憶體 buffer。book 來自第二個 `FundingBookStore`（獨立於 bot 的那份；WS 為主，沒有 baseline 時才 REST reconcile）。trades 以公開 WS `trades` 頻道為主（`external/bitfinex/funding_trades_ws.py`；只算 snapshot 與 `fte`，`ftu` 是同一筆的確認），REST `/v2/trades/f{SYM}/hist` 只在每次（重）連線後補缺口（從 watermark 停下的時間起，以 trade id 去重）；公開 REST 額度與 live bot 共用同一個 IP。每筆 trade 以 id 去重、只收一次。

事件時間 watermark（`MarketFeed.complete_through(symbol)`）：feed 只宣稱它能證明的完整性。連線中且該 symbol 沒有缺口時，每個 frame（含 heartbeat）在本地時間 T 證明完整到 `T - watermark_lag_ms`；斷線時 watermark 停住，缺口補完才繼續；首次補完前是 None。venue 只消費 `(market_through, min(now, watermark)]`，`market_through` 也只推進到那裡（None＝這次不消費、不動）；放單不會把它推過還沒消費的區間，晚到、發生在新 offer 放單之前的 trade 只算給當時就在簿上的 offer（`placed_ms`），而 fill 的時間戳不早於 venue 已經讓外界看過的時間。來源失敗只會讓 buffer 變舊：舊 book 得「no market data」（內部失敗），watermark 凍結，不會編造資料。

`internal_failures`／`unexpected` 是有上限的記憶體 log（最新 1000 筆）；soak 讀 Prometheus 計數器 `bfx_sim_venue_internal_failures_total{kind}`、`bfx_sim_venue_unexpected_requests_total`、`bfx_sim_venue_feed_failures_total{source}`（venue 只透過注入的 `VenueObserver` 回報，不 import metrics）。`Daemon.run` 先啟動 venue 的 task 並等第一批 book（最多 30 s）才啟動其餘 sub-task。simulated 組裝預設不帶 faults；soak 以 `BFX_SIM_FAULTS`（`apps/sim_faults.py`，例如 `unknown_5xx=0.01,unknown_placed_lost=0.005,history_error=0.005,seed=N`）打開種子化的 `FaultPlan`，Bitfinex 組裝見到它就拒絕開機，故障只在行程內的 transport 發生。機率規則以 `(seed, 規則, 目標, 請求 nonce)` 抽籤（nonce 持久且只增不減，重啟不重演）。每一次注入先以 `fault_injected` 事件（種類、目標、請求序號、nonce、時間；submit 另記 symbol／amount／rate／period）append 到 `sim_venue_event`，所以跨重啟存在；內部失敗與 unexpected request 也各以 `internal_failure_recorded`／`unexpected_request_recorded` 事件（有上限）持久化。事件 schema 版本是每個類型各自的（全部 v1，新類型不動既有 log，舊 reader 遇到未知類型就拒絕）。`scripts/sim_soak_report.py` 只靠 DB 區分注入與自然發生的 UNKNOWN（內容相同且時間落在 attempt 區間內），並讀這些持久事件算內部失敗。測試經 `VenueSeam`（`build_daemon(venue_seam=...)`）注入 feed 與 `FaultPlan`（優先於環境變數）。soak 另讀 `bfx_sim_venue_feed_stream_total{event}`（公開 trades WS 的連線與斷線）與 scrape 時計算的 `bfx_sim_venue_feed_watermark_lag_seconds{symbol}`（watermark 未知時沒有樣本）；live bot 的 `bfx_venue_rest_rate_limited_total` 是 HTTP 429 的專用計數（同時仍計入 `http_4xx`），abort rule 靠它。重啟後 feed 的第一次 trades backfill 從 venue 記錄的 `market_through`（只含仍有 resting offer 的 symbol）開始，不是固定 1 小時；沒有 resting offer 的 symbol 仍用固定的啟動視窗。一次補不完（fetcher 擲 `TradesTruncated`）時，watermark 只推到已取得的最後一筆之前，缺口保持開啟並從那裡接續（`source="trades_truncated"`）；停機超過 `retention_ms` 時從保留期下限開始並明講（`source="trades_beyond_retention"`），不悄悄截掉。soak 的執行步驟與判定見 `docs/runbooks/simulation-soak.md`，門檻（含活動下限）見 ADR 2026-10-03 的修訂。

時間：`SignalEngine`、command gate／middleware 的 `date_provider`（`datetime.fromtimestamp(clock()/1000, UTC).date()`）與 `BitfinexLiveExecutor`（`clock`、`date_provider`、cancel 時間戳）都來自組裝時鐘 `now_ms_utc`；telemetry 的 ISO 時間戳仍是牆鐘。

`scripts/bootstrap_simulation_db.py`（owner 執行、冪等）：資料庫必須已 migrate 並 stamp 為 `shadow`（realm stamp 與各表的 realm trigger 是唯一權威，腳本不另外掃內容）；然後在同一個 transaction 內補 `ledger` epoch、建立 `simulation` 帳戶（無 credential 列；label 只是描述）、在 scope lock 下宣告式寫入每個 symbol 的目標 `CapitalPolicy`（`--symbol` enabled，含單筆上限與五個包絡選項；`--disabled-symbol` disabled；與已套用的 digest 相同就不寫 revision；能不能 enabled 由 `write_policy_revision` 決定）。

**Legacy closure seed（`apps/ledger_seed.py`，S1-4d，dormant，owner 執行一次）**：S1-7 halt 中、切換 epoch 之前，把最後一筆 legacy accepted snapshot 的 closure 寫成 ledger 的期初狀態（F1=A）。deploy／compose／systemd 都不引用它（`tests/architecture/test_ledger_seed_dormant.py`）。先在任何 transaction 之外（autocommit）檢查 `--authorize-seed` + manifest、DSN 是 ledger 表 owner 且非 runtime role，以 session lock 拿下每個 scope 的 daemon writer lock 與 transaction lock（直到結束才放），再確認沒有 `bfx_bot`／`bfx_webapi`（含其成員）session；之後才開 REPEATABLE READ transaction，snapshot 因此晚於這些檢查，在其中確認 realm stamp、最新 epoch 為 `legacy`、scope 內沒有任何 ledger 列 → `execution/ledger_seed.py` 讀 closure（classification 照存的讀，不重算；`LEGACY_ATTRIBUTION` 對照表把 legacy credit basis 映到 `attribution_basis`）→ `ledger.seed.write_seed` 先以 `table_digest.digest_rows` 算出預期 digest 再寫：clock（0）→ query → `origin='legacy_seed'` observation（accepted）+ live offer／credit 明細 → seed basis（`attempt_seq_high_water` = 最大 seeded seq、conservation `baseline`、legacy reflected／settled／unresolved；final fence 之後的 legacy tail 記為 unresolved，交給第一筆真實 basis 判）→ attempts（`attempt_seq` = legacy intent 的 `event_seq`、`policy_revision_id` NULL、`seed_provenance` = legacy 列；claim-only live offer 合成 attempt：amount 取自 claim，rate／period 取自 claim 的 decision（legacy 撤單用的條件）並與觀測到的 offer 比對，不符就拒絕，type／flags 照觀測值，所以切換後 ledger 仍能撤單／重定價）+ transport outcome → quarantine（`quarantine_id` = legacy uncertainty id）→ mirrors；同一個 transaction 內再由 `execution/ledger_seed.py`（request 表的 owner）把 pending `uncertainty_resolution_requests` 以 `superseded_by_authority_switch` 失敗（其他 request 表照舊；兩者的 request id 都寫進 JSONL evidence）。寫完把 transaction 切成 READ ONLY，以 `digest_ledger` 重算比對，任何差異整筆 rollback（exit 2）。拒絕（exit 3）：resolved-but-unreflected uncertainty（F5）、沒有 outcome 的 legacy attempt（F6）、沒有 venue opening 的 credit／group（無法以 (symbol, period, opening) keying）、epoch 已是 `ledger`、ledger 非空、runtime session、realm 不符。seed 的 id（query、observation、basis、明細列）是 scope 與 legacy snapshot 的 uuid5，同一個 closure 在 DR 副本與 prod 上寫出同樣的 bytes。宣告的差異（F7）：seed 時仍是 `recent_fill` 的群組在 ledger 以 carry 保持多 cell，直到 credit 到期。

**Cutover comparison（`apps/capital_comparison --mode cutover`，S1-4e，dormant）**：S1-7 halt 中、seed 與 runner 觀測之後、切換之前，在同一個 REPEATABLE READ READ ONLY transaction、以 `SET LOCAL ROLE bfx_cutover_reader`（欄位授權見 `d7e8f9a0b1c2`）跑兩個 arm。`ledger_reader`（F3 (i')）：runner 的同一批 REST 回應以 legacy 自己的觀測結構（`VenueSnapshotObserved`，`--observation` 檔）交給 `execution/capital_observed_baseline`：`CapitalRepository.evaluate_observation_read_only` 跑 acceptance 的 `_validate_snapshot`／`_classify`／歷史 intent 證明，`read_observed` 跑 `_read_capital` 的 basis 與 tail fold，兩者與 `accept_snapshot` 及授權讀取共用同一份程式，不寫任何列、不拿鎖；另一側是 `LedgerCapitalReader`。觀測檔另載 runner 寫入的 ledger observation（query id、observation id、兩個 digest），比對前先綁定：該 observation 必須是 scope 最新的 query、`origin=venue` 且 accepted、digest 與 query 時間相同、以及 legacy 會讀到的欄位（live offers、credits、終態 offer history、coverage 旗標與 history 視窗、funding wallets、offer 的 `mts_created`；對照表 `BINDING_OF_LEGACY_FIELDS`，由測試從 legacy 原始碼收集欄位釘住）與檔案的第一筆觀測完全一致，否則該 scope `not_comparable`（`observation_binding_mismatch`）。S1-5 的限制：runner 檔要從 ledger 以精確 decimal 解析出的列產生（legacy 的 `resp.json()` 是 float，超過 17 位有效數字就會綁定失敗）；窗口從所有帳戶最早的 query start 起算，所以 runner 要平行觀測各帳戶並立即開始 C（或在 runbook 限制帳戶數）。兩邊都以 manifest 的 as-of `now_ms`（必須等於觀測完成的時刻）判斷；欄位對照為 fold arm 的結果欄位去掉 `query_id`（legacy 不存 query）、ledger-only 欄位與 fence。F7 差異只在「把 seed `recent_fill` 群組多出的 cell 曝險加回 legacy 後，以 legacy policy 重算的每個欄位都與 ledger 相等」時記為 `declared`（附群組證據），其餘一律 `different`；halt 中成交在兩邊歸屬同一 cell，沒有其他宣告。`closure`：seed evidence JSONL 對 legacy closure 的 anti-join（live offers ⟷ ack attempt ⟷ seed 觀測／mirror、未結 attempt 與 outcome kind、open uncertainty、credit 群組與 cell set、symbol 數值、`seed_provenance`、fingerprint（差異需分類）、`trading_state` 最新列、request 失敗／延續、legacy projection 的 live symbol 須有 cell），每項雙向；mirror 一項在 capture point（scope 最新 query 仍是 seed 的）比對 live mirror 與 legacy 兩個方向，runner 觀測之後只能檢查每個 legacy live offer 有 mirror 列、以及 legacy 不認得的 live mirror 列都出自 runner 的觀測，所以較強的 capture-point 模式要在 halt 內的隔離 DR 副本上、seed 之後立即先跑過，再對 prod 執行（不帶觀測檔的呼叫方式由 S1-5 的 halt 編排接上）。另以注入時鐘檢查所有帳戶中最早一筆觀測的 query start（legacy 分類的是第一筆觀測）到結束 ≤ 300 秒。任一 arm 未過、inventory 不乾淨或超時都 exit 1。

**歷史相容性**：舊 `canary` phase、fUST cap10000／fUSD cap0、
`BFX_BALANCE_BUFFER_USDT` 與 env-based canary profile 已退役，不是現行資金 authority。
`canary` 已從 `Phase` 移除，`BFX_PHASE=canary` 在 `load_config` 直接拒絕；研究腳本改讀 `cells.live.yaml`。
每個 build 的四步 canary ceremony（release sessions、canary permit、halt epoch 續開、
Halt 2 DR 收據）已由 ADR 2026-09-25 的 release flow 取代並刪除，資料見 `release_archive`。

**Phase ⟷ Realm guard**（`load_config()`）：live 禁止 shadow realm；
production 使用 prod，ci 僅供測試。shadow 禁止 prod（模擬不可污染真錢分析）。
違規 `ValueError` fail-fast；webapi 必須明確設定與 daemon 相同的 realm。

**Cells**：`cells.yaml`（shadow，多對跨 fUSD/fUST 與 p2/p30/a30）；本輪
`cells.live.yaml` 只有兩個 armed mean_reversion fUST cells（a30/p2），fUSD
保持 dark；RatePercentile 與 p30 不在 live set。`shadow-p14` 專用
`cells.experimental-p14.yaml` 鎖定 AdaptivePeriod `p_mid=7`、`p_long=14`、`t1=0.5`、
`t2=1.5`，並且 profile 固定 `BFX_PHASE=shadow`、`BFX_DEPLOYMENT_ENV=shadow`、
`optimizer_shadow`；live profile 不得選用它。單筆送單金額不在 yaml 設定——由
deployment reconciler 依 gap 動態決定。

**基礎設施**

| 服務 | 平台 |
|---|---|
| Backend daemon | VM 自托（`<vm-host>`，Docker，Python 3.13 + uv；`bfx-bot`/`bfx-webapi`。Koyeb 已於 2026-05-31 cutover 至 VM） |
| Database | VM 自托 Postgres 18（`bfx-postgres`。Neon 已於 2026-06-23 棄用歸零） |
| Cache | VM 自托 Redis 7（`bfx-redis`；Better Auth session/rate-limit 用，daemon 不依賴） |
| Frontend | VM 自托（Next.js standalone，只綁 host loopback `127.0.0.1:3001`，由 VM 上的公開 ingress（TLS 443）反向代理；ingress 設定見 `docs/runbooks/`。Vercel 專案已刪） |

**跨 migration 的契約（`core/schema_head.py`）**：serialized projector 的 seeded cursor 與 projection archive 各由一個 migration 建立；之後每個 migration 以模組屬性 `ledger_contract = "preserved" | "changed"` 宣告是否改變它，契約成立的 revision 集合由此推導（從 head 往回到最近一個 changed，或到建立它的那個），不再有人工維護的清單。`tests/test_schema_head.py` 拒絕沒宣告的 migration 與多個 head。

**部署（`deploy/vm/ops/bfx_deploy.py`，ADR 2026-09-25 ci-registry-digest-deploy）**：CI 在綠燈的
`main` commit 建 arm64 image 並推到 GHCR；VM 只以 digest 拉取、從不建置；先備份再 migrate，
recreate 後查健康，失敗就回到前一個 digest，每次結果寫進 append-only `deployments` ledger。
部署閘門：target digest 必須是 `backend:main` 且與 `backend:sha-<rev>` 相同、`<rev>` 在 `origin/main` 上；
有 pending migration、diff 讀不到、或改到 DR 路徑（`DR_TRIGGER_PATTERNS`：`deploy/vm/pgbackrest/**`、
`deploy/vm/postgres/**`、`docker-compose.bot.yml`、`docker-compose.dr.yml`，寫死在執行中的工具裡，commit
無法關掉自己的觸發）時，先跑該版本 DR 腳本的 isolated restore test；有 migration 時先停 bot 並備份。
health wait：bot `/healthz` 300 s、webapi 120 s、frontend `127.0.0.1:3001` 120 s，再 60 s settle。
已套用 migration 後失敗不回滾（舊碼未必能跑新 schema），停 bot、roll forward。
部署工具注入 `BFX_IMAGE_DIGEST`／`BFX_SOURCE_REVISION`／`BFX_DEPLOYMENT_ID`：只用來標示每筆
execution audit 的 `config_hash`／`service_version` 與狀態報告，部署永遠不改變 trading state（§6）。
live daemon 開機時（讀憑證與任何交易之前）比對 image 內 `alembic/` 推導出的唯一 head（`core/schema_head.build_head`；多個 head 也拒絕開機）與資料庫的
`alembic_version`：不一致代表這個 build 不屬於這個 schema（例如回滾到較新的 schema 上）。
讀不到已套用的 CapitalPolicy 也一樣：`safety/boot_stop.py` 只發 critical 告警
`venue_offers_may_remain`，然後拒絕開機、由 supervisor 重啟。什麼都不寫（停機狀態會比原因活得久，
而部署從來不是交易決策，D5），也不碰 venue（拒絕開機的 process 沒有 command gate 能證明哪些 offer
是自己的，D2/D3）；要撤就用 operator kill。

**Application authority**：Overview 的 funding-status adapter 沿用 authenticated
account proxy/MFA，webapi 只需既有 membership/account SELECT grants，向 daemon
讀 canonical status＋dry-evaluate；不讀 auth.user、不推算另一套 budget。
缺 policy、disabled、halt 分別顯示；Decimal 保留字串，draft 不等於 applied。
**Operator requests（`execution/operator_requests.py`，ADR D4'）**：所有會改動執行狀態的人為操作——
uncertainty 裁決（bind-to-venue／mark-not-accepted／manual-resolution，`uncertainty_resolution_requests`）、
resume／kill（`trading_control_requests`）與幣別 enable／disable（`capital_policy_requests`）——走同一套 outbox 合約：webapi 只以
`insert_request` 寫該表的請求欄位（model 的 `REQUEST_COLUMNS`＝migration 的欄位級 INSERT grant）並回 202，
不取帳戶鎖（單一 pending 由 partial unique index 保證）；daemon 的 `OperatorRequestWorker` 子類
（`UncertaintyResolutionWorker`、`TradingControlWorker`、`CapitalPolicyRequestWorker`，各自一個 task）一次處理最舊的一筆，在帳戶鎖內的 savepoint 以
`operator_authorized`（SQL `public.operator_authorized`）重驗權限後才 apply，結果（applied／rejected＋原因碼／
failed＋根因）記回請求列；寫不進去的請求另以獨立交易標 failed，連這都失敗就由本 process 跳過，不擋佇列。
需要在鎖外觀測的資料以 `NeedsPreparation` → `prepare` 取得後再 apply。清單列帶最新一筆請求，前端只在有 pending 時輪詢清單。webapi 對 ledger／
projection 表零寫權限，授權與收回都在 migration（`1c435a35dcb4`、`5b1e7c9d2a40`、`7d2a9c4e6b13`）。
靜態 admin token 不能 resume live。TOTP 真實 enrollment／production acceptance
仍是人工作業，technical start/health 不等同 activation。

**關鍵 env vars**：`BFX_PHASE`、`BFX_DEPLOYMENT_ENV`、`DATABASE_URL`、
`BFX_EXCHANGE_ACCOUNT_ID`、`BFX_VAULT_KEK`、
`BFX_WS_CLIENT_ENABLED`、`BFX_FILL_TRACKER_ENABLED`（Bitfinex 需要前者；simulated 兩者都必須關閉）、
`BFX_SIM_INITIAL_WALLETS`、`BFX_SIM_FAULTS`（simulated 專用，Bitfinex 組裝設了就拒絕開機）、
`BFX_RECONCILE_INTERVAL_S`、`BFX_QUOTE_TTL_MS`、`BFX_SCHEDULER_BUFFER_S`、
`BFX_SAFETY_CONFIG`、`BFX_CELLS_YAML`。已退役、設了就拒絕開機：`BFX_EXECUTOR` 與舊資金 env（`BFX_ALLOCATION_CAP_USDT`、`BFX_BALANCE_BUFFER_USDT`、`BFX_CONCENTRATION_PCT`、`BFX_VENUE_FLOOR_USD`、`BFX_MIN_OFFER_BUFFER_PCT`）。
Bitfinex secret 不再從 `BFX_API_KEY`/`BFX_API_SECRET` 讀取；由 account-owned
credential vault 解密。public read model 另以明確的
`BFX_PUBLIC_EXCHANGE_ACCOUNT_ID` 綁定 UUID。

### 量測自動化（E3，2026-07-06）

- **Weekly chain**：VM systemd timer `bfx-weekly-report.timer`（Mon 04:17 UTC，unit 檔在 `deploy/vm/systemd/`）→ `bfx-weekly-report.service` 以主機工具的 `deploy/vm/ops/bfx_weekly_report.py` 跑 compose one-shot `weekly-report`（`deploy/vm/ops/docker-compose.weekly-report.yml`，隨每次部署安裝；image＝ledger 的 backend digest）：`ingest_funding_stats`（AlwaysFRR arm 資料）→ `run_weekly_attribution`（per-cell fee-adjusted APR → `attribution_weekly` 表，全量重算 delete-then-insert；2026-09-27 起來源是 bot 的 `CreditHistorySync` 每小時同步的 `funding_credit_history`／`funding_trades`（venue credit/loan history 的 實際持有時間（每筆自 max(MTS_OPENING, MTS_CREATE) 起算，loan 轉成的 credit 不重複計），以 (symbol, period, MTS_OPENING) 對到 MTS_CREATE 同一時刻的 trade（rate／amount／id 不是 key：credit rate 精度不同，loan 會轉成新 id、拆分金額的 credit），trade 的 OFFER_ID 對回 execution decision 的 cell，對不上歸 `unattributed`；authority 切到 ledger 後新 offer 只存在 ledger journal、open credit 只在 `venue_credit_mirror`，loader 經 `modules/ledger/attribution_reads.py`（公開讀 port，ack outcome 的 attempt → cell；mirror 的 open credit）與 legacy 來源取聯集，同一 offer 兩邊 cell 不同則列入報告的 `OFFER CELL CONFLICTS` 並歸 `unattributed`，不擇一），不再用 ORDER_FILL×held-to-term 或 CREDIT_CLOSED 的 rate/period/close；同一步輸出每週「credit 推得的 net 利息 vs `funding_interest_payments` 帳本」對帳 `<date>-attribution-reconciliation.md`，帳本窗 = credit 週往後平移一天（每日利息隔日 ~01:30Z 入帳），|diff| > max(5%, 0.01) 標 FLAG；規則見 `modules/live_validation/credit_attribution.py`）→ `report_interest`（帳本週報）→ `run_g3_live_validation`（報告 → VM `~/bfx/reports/<date>-g3-live-validation.{md,json}`；2026-09-27 起 active arm 同樣用 bot cell 的 venue credits × 實際持有時間（按日曆週切分；分到多個 cell 的 credit 依上述 amount 分配的比例計入各 cell），C = 帳本 funding wallet 餘額（`--capital` 可覆寫），信任 gate 由 deployment/NAV anchor 改為上述帳本對帳：只看最近 8 個已結算週，其中 FLAG → UNRELIABLE，除非 operator 以 `--ack-week YYYY-MM-DD=理由` 確認（加進 `deploy/vm/ops/docker-compose.weekly-report.yml` 的 G3 指令，隨部署生效並留在 git，報告回顯），更舊的 FLAG 只列出不擋；MR-alpha 每個 bot cell 對自己 period_agg 的市場利率序列、另有 total 列（缺序列的 cell 標 unavailable、不進 total，total 註明覆蓋的 capital-days 比例）；資料門檻 = 評估窗平均帳本錢包餘額 × 7 天；over-deploy clamp 移除改報 peak；此前的 G3 報告與之後不可比，報告內有 Methodology change 段。程式在 `modules/live_validation/{attribution_loader,g3,g3_report}.py`，scripts 只是 CLI）。報告不進本 repo。
- **研究重驗（Proposal E，2026-09-22）**：同一個 `weekly-report` 在 G3 之後接研究步驟，每步 `timeout` + fail-soft、絕不阻斷營運報告：外部訊號 topup（`ingest_perp_funding`、`ingest_liquidations`）→ `learn_book_fill_rate`（book-replay artifact，`source="book"`）→ `run_period_structure_backtest`（`--fill-model book` 與 linear 各一份）→ `run_oos_profitability` → `diff_research_report`（champion 漂移＝最近 12 窗中位數跌破全歷史 p25；challenger 反超＝配對 CI 下界連續 3 週 >0；只開 registry review，永不 auto-promote）。報告落 VM `~/bfx/reports/<date>-{period-structure-book,period-structure-linear,oos-profitability,weekly-research-*}.md`。systemd `TimeoutStartSec=5400`。
- **儀表**：webapi `GET /api/v1/exchange-accounts/{exchange_account_id}/attribution/weekly`（`bfx_webapi` 需 `GRANT SELECT ON attribution_weekly`，非 migration）→ FE `/attribution` 頁三線圖（bot net APR / always-close / AlwaysFRR）。webapi 與 weekly job 必須使用同一個 account UUID 與 `BFX_DEPLOYMENT_ENV`；不再依 process-global `BFX_ACCOUNT_ID` 選 scope。
- **歷史政策（per-symbol cap 加碼 gate，非現行 all_available authority）**：舊政策要求調高（已刪除的）`safety.canary.yaml` 的 `allocation_cap.caps[symbol]` 前，最新 weekly G3 verdict = PASS **且** AlwaysFRR benchmark spread 非負（`frr_benchmark` unavailable 時不加碼）。fUST 3000→10000（`aa4842c`）是在 INSUFFICIENT_DATA 上拉的；保留此失敗歷史與 G3 績效證據。現行沒有提高固定 cap 的操作，也不能把 policy conversion 或 canary 成功解讀為績效通過。
- **FRR 單位**：AlwaysFRR arm 的 rate = `funding_stats.frr × 365`（≈ ticker per-day FRR，2026-07-06 實測誤差 <0.5%；`live_attribution.FRR_ANNUALIZATION`），換算後必過 `assert_market_rate_band`。`funding_stats.frr` 原值仍非市場利率（ADR 2026-05-28 不變）。

---

## 9. Key Invariants

- **I-SW 單一寫入者曝險**：`DeploymentReconciler` 是 venue 提交的唯一寫入者；訊號層只寫 StandingQuote。所有 exposure mutation 由 account event writer 序列化；snapshot 絕對設定 `offered/lent`，lifecycle event 只做有序增量。
- **I-VTA venue 即真相**：`BootRecovery.run()` 以不帶 symbol filter 的 REST offers + credits + wallets 建立 `VENUE_SNAPSHOT_OBSERVED`；外來 offer 只記錄與告警、missing 釋放、stale PENDING 進 UNKNOWN，並以衍生 `PositionReconciled` 絕對覆寫 ledger。WS 是延遲最佳化；90s 迴圈保證收斂。
- **I-CAP canonical capital authority**：每 account/environment/symbol 獨立計算。A=venue available，L=尚未證明反映於 snapshot 的 durable commitments，R=applied reserve，T=canonical available+offers+lent（去重），E_cell=該 cell 的 offers + unreflected commitments + 歸屬該 cell 的 active credits。credit 的歸屬在 snapshot 接受時（fence）決定並寫進 classification 的 `credit_cells`，authorization read 只查表、不問 venue：以 (symbol, period, MTS_OPENING) 分組 active credits/loans（snapshot 保留 venue [13] MTS_OPENING；rate 精度與 trade 不同，loan 會轉成新 id／新 MTS_CREATE／拆分金額的 credits，但 MTS_OPENING 不變）：(1) 已同步的 `funding_trades` 中 MTS_CREATE 等於該 opening（同 symbol/period）的 trades，OFFER_ID→offer_claims→execution decision→cell，組內每筆都計入所有這些 cell（trade 指向非我方 offer 則為 U；組內金額超過已同步 trades 金額時兩邊答案都算）；(2) 前一個 accepted snapshot 已決定的同一組沿用（loan 轉 credit 會換 id 與金額，所以按組而非 id；補足 trade 每小時同步前的空窗）；(3) 首次出現且尚無 trade 的 credit，歸給本 snapshot 可見、可能產生它的我方成交（active offer 已成交部分、terminal offer history、上一個 snapshot 還在簿上而今消失的我方 offer）：同 symbol、金額 ≤ 成交量、offer 建立不晚於 credit 的 opening；不要求 rate/period 相同，候選有多個 cell 就每個都計入（寧可暫時多算，不讓借出的錢離開 cell 曝險）。都不成立的是 unattributed credits（U）：只在 T 算一次、不計入任何 cell（2026-09-27 起；先前 U 保守計入每個 cell，U 接近 cell_limit 時把 venue 最低單額擋在 headroom 外而閒置現金）；外來 offer（D2）不進 T 也不進 E_cell；未知 attribution 不捏造 ownership。無 `credit_cells` 的舊 classification 回退為 U 計入每個 cell，直到下一個 snapshot。
- **I-SP spendable**：`spendable=max(0,A-L-R)`；每 tick 多 cells 共用此 pool。planner、command admission、status 共用 evaluator；同 account lock/transaction 內重查 policy revision、snapshot fence、guards 並建立 intent，不靠 in-memory tracker 授權。
- **I-CC concentration**：`cell_limit=max(0,T-R)*0.70`，`cell_headroom=max(0,cell_limit-E_cell)`，`new_offer_amount≤min(spendable,cell_headroom)`；單一 active cell 也固定70%。reserve 增加或資金下降不召回貸款，只阻擋超限新單。金額向下量化並通過 adapter minimum/precision；不足 minimum 就 block，不增加金額跨越 headroom。
- **歷史／simulation 說明**：舊 `allocate_gap`、reserved-only tracker rescale、固定 cap 與153 dust threshold 不是 live authority；`0d29fc8` 的單 active cell100% relaxation 已移除。相關歷史及 G3 未通過結果保留，不作新命令授權。
- **I-BOOK original decision validity**：READY 綁定定價所用 immutable book snapshot、symbol、sequence/checksum consistency 與 provider freshness bound。account lock／identity hash／guard 等待完成後以 current clock 重查，adapter 在 request 前再檢查；失效落 durable NOT_SENT，保留 intent，不用另一份新 book 偷換原價格，不自動重送。
- **I-WAI write-ahead intent**：txn1 寫 `ReservationIntent`(PENDING) → REST（唯一非事務邊界）→ txn2 寫 typed outcome；crash 於中間留 PENDING，boot 時進 UNKNOWN，不得盲目重送。txn 永不跨 REST call。
- **I-IDEM idempotency**：`ORDER_FILL` / `RESERVATION_RELEASED` 以 dedup key 去重；`OfferRegistry.transition()` 純函式、原子套用、重送安全。
- **I-ES event sourcing SoT**：`event_log` append-only；snapshots 皆可由 log 重算；bus publish 為 best-effort，recovery 一律走 event_log。
- **I-EW serialized projection**：所有 live execution append 先持有 account/environment transaction advisory lock；cursor 之後的 event-log 缺口按 `event_seq` replay，event、entity snapshots 與 `projection_heads` 同 transaction commit，舊事件 dedup 不得令 cursor 倒退。Alembic-managed DB 未完成 cursor seed 時 fail closed。
- **I-UA ambiguous submit**：timeout、connection reset、5xx、malformed response 或 restart 後未完成 intent 一律 `ReservationUnknown`；不自動 retry，只隔離該幣別。UNKNOWN 的 intended amount 進 `uncertain_amount`，只由 fresh full-account evidence（金額指紋比對，D3a）或明確 operator resolution 移除。
- **I-FO foreign offers（lending envelope D2）**：active venue offer 無本地 provenance 時是外來 offer：計入 ledger 的 `offered_amount`（它確實在 venue 上）、不算受管曝險、不合成 CID/reference、不指派 strategy、bot 不撤不重定價，只發 `foreign_exposure` 告警；舊的 `VenueOfferQuarantined` 事件只在重播時投影。
- **Projection clean cutover**：在 account/environment 的 persistent halt 下，preflight 必須核對 backup/isolated-restore、migration/schema、event head/hash、config/image/projector 與 snapshot fence evidence；舊 projection 只作 diagnostic，empty projection 一律由 event chain 重建。未解 `PENDING` 明確轉 `UNKNOWN`，orphan 保留 venue object 並 quarantine；任何 uncertainty、hash/diff 不一致或 coverage 缺失都不得放行。
- **Rollback after a venue write**：rollback 不得把 DB restore 當作 venue rollback：維持 halt、fresh full-account reconcile、adopt 精確 match 或 manual resolution，然後 forward-fix。DB restore 只在量測證明 no later venue mutation occurred 時才可能安全；UNKNOWN 永不 automatic retry。
- **I-CI candle 不可變**：進入 `strategy.observe()` 的 candle 必須 `is_final=true`，且定稿後其值永不改變——`upsert_candles` 的 UPDATE arm 帶 `where is_final = false`，對已定稿列無論來源（WS 重送、REST 回補）皆為 no-op，差異改寫進 `funding_candle_revisions`。封存有兩條互補路徑：(a) **寫入即判定**——`mts` 早於 `now` 所屬期者落地就是 final（REST backfill 寫的全是已結算歷史，若落地為未定稿，final-only 讀取會看不到，`fetch_and_store` 的寫後讀回也會回空）；(b) **期轉換時補封**——`CandleWriter` 見到同 series 更晚的 mts 就封存前一根，處理「寫入時還開著、之後才過期」的那根。都不是等固定秒數。**兩個讀取函數必須同規則**：`get_up_to` 與 `get_candles_in_range` 皆預設 `final_only=True`——只改前者時，warmup（走後者）會吸進形成中的 candle 而 replay 不會，兩臂差一次 `observe()` 就是 `ema_span=24` 下約 0.7% 的 EMA 位移。理由：決定性重放要求輸入不可變；輸入可變時 live 增量狀態與 replay 重建必然分歧，而 `LendDecision.rate` 直接取 `candle.close`，失真值會成為實際掛單利率（2026-07-27 實測 23/132 slot 被事後改寫、最大 -35.3%，掛單價偏離達 +54.6%）。
- **Submit outcome discipline**：先 durable 寫入 `ReservationIntent` 再發送 request；local pre-transport validation 產生 `ReservationFailed`，明確拒絕才產生 `ReservationFailed`，timeout/transport ambiguity/malformed response 一律 `ReservationUnknown` 並 fail closed，不得以 exception 直接推論 venue reject 或自動重試。只有 `CLAIMED`/可稽核 accepted outcome 才 `record_deploy`。
- **fail-closed**：任何 guard timeout（2s）或 exception → `allowed=False` + `safety_trigger(critical)`。
- **TaskGroup 監督**：任何 sub-task 例外 → ExceptionGroup 傳播 → daemon 非零退出，無 silent task death。
- **I-RP reprice-down only**：sweep 只砍「高於現行 active quote 超過 tolerance 且夠老」的 offer；不砍低於 quote 的、不砍 spike 當小時的（min-age）、無 active quote 不砍。cancel 失敗 fail-safe（offer 留在 book）；`ExecutorAuthError` propagate。sweep 不直接改 ledger/tracker。
- **I-PUB public read model（`modules/api/public.py`）**：`/api/v1/public/proof-summary` 與 `/api/v1/public/funding-rates.csv` 不需驗證、不掛 per-user rate limiter，回應帶 `Cache-Control: public, max-age=3600`。只輸出百分比（週 APR、`close_apr_pct`），絕不輸出絕對金額（資金規模、利息金額、capital-days 只在內部加總用）；CSV 的 `symbol` 以 `Literal["fUST","fUSD"]` 白名單驗證（其他值 422），只讀 1h／`p2` candle。proof account 由 `BFX_PUBLIC_EXCHANGE_ACCOUNT_ID` 明確指定，未設定回 503，不 fallback。
- **I-EG execution eligibility**：只有已 audit 的 `ReadyToSubmit` 能到 executor。每個 live-capable candidate 都需 fresh、symbol-matched、**sequence/checksum-consistent**（有完整 baseline 且自該 baseline 起未偵測到 gap 或 mismatch，見 7c）的 exact-period book evidence；缺少 period、depth、model、safety 或 audit 時，產生 typed blocked outcome，絕不重用 signal quote、scalar ticker 或 linear estimate。

---

## 10. Related

設計決策紀錄（ADR）另行私下保存；本文件只記錄現行、由程式碼驗證過的規則。
