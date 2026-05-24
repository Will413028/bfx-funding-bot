---
title: Postgres Event-Store SoT Migration (Axiom → PG Hybrid)
date: 2026-05-23
status: draft
phase: 4.4-prework
related-pending:
  - "Axiom-as-SoT 三大問題:30d retention cliff、indexing latency、Axiom down = daemon won't boot"
related-spec:
  - docs/superpowers/specs/2026-05-18-phase4.1-paper-shadow-infra-design.md  # §Q9 正確分層原則
  - docs/superpowers/specs/2026-05-21-phase4.2-design.md                      # ledger 決策 (drift 來源)
  - docs/superpowers/specs/2026-05-23-axiom-ci-env-separation-design.md       # T12 / deployment_environment
---

# Postgres Event-Store SoT Migration (Axiom → PG Hybrid)

## 1. Context & Motivation

事件溯源的 source-of-truth 目前落在 **Axiom**(log-analytics SaaS)。這違反本 repo 自己在
phase-4.1 §Q9 立下的分層原則 —— *「log 平台做 diagnostics,relational DB 做 transactional
invariants」*。phase-4.2 ledger 決策(`2026-05-21-phase4.2-design.md:244-261, 404-407`)把
transactional invariant 放到了錯的層。

Axiom-as-SoT 帶來三個結構性問題:

1. **30d retention cliff** — `BFX_EVENT_REPLAY_DAYS` 預設 30,更舊事件被 evict;且已撞到
   free-plan dataset 上限。
2. **Indexing latency** — Axiom 索引延遲 2–15s,寫後無法即時讀回(read-your-writes 不成立)。
   測試端用 `_poll_until_indexed`(T12 helper)遮蓋;live daemon 則直接 timeout 硬失敗。
3. **Boot coupling** — `PaperPositionLedger.replay_from_axiom()`(`daemon.py:702`)是 blocking
   + 3 retries,失敗即 `LedgerReplayError` → exit 1。**Axiom 掛掉 = daemon 起不來**,核心安全狀態
   被一個外部 SaaS 綁死。

系統處於 pre-launch(產品 concept/dev、無付費),可承受最佳實務重構而非 minimal workaround。

### 現況盤點(遷移面)

- in-memory projection:`PaperPositionLedger`(`reserved_usdt`/`realized_usdt`)、`OfferRegistry`
  (offer FSM:`venue_offer_id → CLAIMED/RELEASED`)。兩者開機從 Axiom replay 30d 事件重建。
- **Postgres 目前沒有任何 ledger / snapshot 表**;state 100% 活在 Axiom + 記憶體。既有 PG 表
  (`funding_candles`、`funding_stats`、SaaS `accounts` 層)皆非 bot SoT。
- Axiom 寫入分兩類:核心 SoT 事件(`RESERVATION_CLAIMED`/`ORDER_FILL`/`RESERVATION_RELEASED`,
  `axiom_sink.py:49-143`)vs 純診斷(decision/safety/health/cancel-audit)。
- 寫入**不具交易性**:REST submit → 發 domain event → bus 非同步 fanout(`bus.py` 自標 transactional
  outbox 為 "Phase 5+")。
- 既有可重用資產:pure `transition()`(`registry_offers.py:77-177`)、`fill_tracker` diff
  (`fill_tracker.py:160-200`)、`cid` 冪等鍵、dedup key `(venue_offer_id, venue_seq)`。

## 2. Decision Summary

| 決策 | 結論 | 理由 |
|---|---|---|
| **SoT 模型** | **Hybrid** — append-only `event_log` + 交易內維護的 snapshot 表 | money-handling 需 audit + rebuild,但 runtime 從不需走完整歷史 → 純 ES 的 runtime replay 是 over-engineering;純 snapshot 又丟掉 audit/rebuild |
| **Axiom** | **整個移除** | SoT 移走後只剩低量 forensic 事件;Twelve-Factor:operational → structured stdout,forensic → PG `diagnostics` 表(可與 ledger JOIN)。真正的 observability 平台等上線再當獨立決策引入 |
| **寫入一致性** | **A2 — write-ahead intent**(cid 冪等 + venue reconcile) | money 路徑 canonical pattern:絕不在無耐久本地紀錄下對交易所送單;cid 可區分「從沒送出」vs「送了 ack 遺失」 |
| **Cutover** | **乾淨切換**,空庫起步,無 backfill | pre-launch、disposable paper/shadow data、Axiom 已在 evict;開機對 venue reconcile 即可安全重建;一次性 importer 屬 YAGNI |

光譜上明確拒絕的兩端:**純 snapshot(B-strict)** 太 lossy(丟 audit/rebuild);**full event-sourcing
(A-strict,runtime replay)** 與 **full transactional outbox(A3)** 對 solo 單實例是 over-engineering
(A3 即 `bus.py` 自標的 "Phase 5+")。

## 3. Architecture

```
                 ┌─────────────────── Postgres (SoT) ───────────────────┐
  Bitfinex REST  │                                                       │
  (ultimate      │   event_log  (append-only, 永久保留)                  │
   truth for     │      │  每筆 mutation 在同一 txn 內                    │
   offers/fills) │      ▼                                                │
        ▲        │   snapshot 表:  position_state + offer_claims         │  ← cold start 只讀這個
        │        │                                                       │
        │        │   diagnostics (decision/safety/audit, 可 prune)       │
   reconcile     └───────────────────────────────────────────────────────┘
   (fill_tracker poll)                          ▲
                                                 │ load_snapshot() 取代 replay_from_axiom()
                          PaperPositionLedger / OfferRegistry (記憶體 projection)

  operational logs ──► structured stdout     (Axiom 整個移除)
```

核心轉變:in-memory projection 的耐久後盾從 **Axiom replay**(30d、async indexing、掛了起不來)
換成 **Postgres snapshot**(一次 SELECT、read-your-writes、自有 infra)。`event_log` 保留完整事件
供 audit + rebuild。

**單元邊界與職責:**

- `PostgresEventStore` — append 事件 + 查詢 + `rebuild_snapshot_from_log()`。取代 `AxiomReplayQueryAdapter`。
- `PaperPositionLedger` / `OfferRegistry` — 記憶體 projection;開機 `load_snapshot()`,mutation 經 store 在 txn 內寫 event + 更新 snapshot。
- `diagnostics_sink` — forensic 事件 → `diagnostics` 表;失敗降級不阻斷。
- 開機 orchestration(`daemon.py`)— snapshot load → resolve PENDING → venue reconcile。

## 4. Data Model

### `event_log` — append-only,SoT 真相來源

| 欄位 | 型別 | 說明 |
|---|---|---|
| `event_seq` | BIGSERIAL PK | 全域單調序,replay/rebuild 順序 |
| `account_id` | text | 多租戶隔離 |
| `deployment_environment` | text | paper/shadow/prod 隔離 |
| `event_type` | text | `RESERVATION_INTENT` / `RESERVATION_CLAIMED` / `RESERVATION_FAILED` / `ORDER_FILL` / `RESERVATION_RELEASED` |
| `cid` | text | client-order-id,**冪等鍵**(A2 用它 resolve PENDING) |
| `venue_offer_id` | text NULL | CLAIMED 後才有 |
| `payload` | JSONB | size_usdt、rate、signal_correlation_id… |
| `occurred_at` | timestamptz | 事件時間 |
| `recorded_at` | timestamptz DEFAULT now() | 寫入時間 |

索引:`(account_id, deployment_environment, event_seq)`、`(cid)`、`(venue_offer_id)`。
**無 UPDATE/DELETE**(append-only;丟掉 Axiom 的 30d 限制,永久保留;retention/partitioning 留待規模再議)。

### `offer_claims` — snapshot:offer FSM projection

key 用 `cid`(PENDING 時尚無 `venue_offer_id`)。

`cid` PK、`account_id`、`deployment_environment`、`state`(PENDING/CLAIMED/RELEASED/FAILED)、
`venue_offer_id` NULL、`size_usdt`、`signal_correlation_id`、`last_event_seq`、`created_at`、`updated_at`。

### `position_state` — snapshot:ledger projection

PK `(account_id, deployment_environment)`、`reserved_usdt` NUMERIC、`realized_usdt` NUMERIC、
`last_event_seq`(high-water mark,供 `snapshot == replay(log)` 驗證 + rebuild)、`updated_at`。

### `diagnostics` — forensic(取代 Axiom 診斷)

`id` BIGSERIAL PK、`account_id`、`deployment_environment`、`kind`(DECISION/SAFETY_TRIGGER/CANCEL_AUDIT)、
`payload` JSONB、`occurred_at`、`recorded_at`。索引 `(account_id, occurred_at)`。**可 prune**(30–90d,非 SoT)。

**核心不變式:** `position_state.last_event_seq == 已套用的 MAX(event_seq)`,且 snapshot 必須等於
`replay(event_log)` 的結果 —— rebuild 與 integrity check 的基礎。

## 5. Write Path (A2 — write-ahead intent)

唯一非交易性邊界是 Bitfinex REST,用「intent 前、outcome 後」夾住。每個 outcome 都是
**event append + snapshot update 在同一 txn**。

**下單(place offer):**
```
txn1:  INSERT event_log(RESERVATION_INTENT, cid, payload)
       UPSERT offer_claims(cid, state=PENDING)                          ← commit
  │
  ├─ REST submit(帶 cid)到 Bitfinex
  │
  ├─ 成功 → txn2: INSERT event_log(RESERVATION_CLAIMED, cid, venue_offer_id)
  │                UPDATE offer_claims SET state=CLAIMED, venue_offer_id
  │                UPDATE position_state SET reserved += size, last_event_seq   ← commit
  │
  └─ 失敗 → txn2: INSERT event_log(RESERVATION_FAILED, cid)
                   UPDATE offer_claims SET state=FAILED                          ← commit (reserved 不動)
```

**成交(fill_tracker poll 偵測):**
```
txn:  INSERT event_log(ORDER_FILL, venue_offer_id)
      UPDATE position_state SET reserved -= filled, realized += filled, last_event_seq   ← commit
```

**釋放/取消:**
```
txn:  INSERT event_log(RESERVATION_RELEASED, venue_offer_id)
      UPDATE offer_claims SET state=RELEASED
      UPDATE position_state SET reserved -= remaining, last_event_seq   ← commit
```

snapshot 跟 log 永遠在同一 txn,**不可能 diverge**。直接消滅「PG state ↔ Axiom 兩系統 dual-write
無 txn」的整類 bug。

## 6. Boot Sequence & Cutover

```
1. 連 Postgres。失敗 → fail fast(正確:SoT 不可用不該起);加 connection retry/backoff
   (3 次 1/2/4s)。差別:自有 infra、無 30d eviction —— 把「掛了起不來」搬到對的地方。
2. load_snapshot(): SELECT position_state + offer_claims                ← O(1),不 replay
3. resolve PENDING: 對每個 state=PENDING 的 cid,向 venue 查
      ├─ venue 上找到該 cid 開倉 → 補成 CLAIMED (+reserved)
      └─ 過 grace 仍找不到 → 收斂為 FAILED            ← A2 crash-mid-flight 復原
4. venue reconcile: poll Bitfinex 開倉(沿用 fill_tracker diff 邏輯)
      ├─ venue 有、本地 CLAIMED 沒有 → 認領 orphan(順便把 size 計回 reserved)
      └─ 本地 CLAIMED、venue 沒有 → 標記 released/filled    ← 讓空庫切換安全
5. ready
```

**Grace window(decision):** PENDING 找不到時,用 Bitfinex offer 狀態查詢 + 保守 grace
(2–3 個 poll 週期,非固定秒數)才判 FAILED,避免「REST 已送達但 venue 索引/回應延遲」誤殺。

**Cutover(decision):** 第一次切換 Postgres 空庫 → step 2 讀到空 snapshot → step 4 從 Bitfinex
實際開倉重建 registry,**並把開倉 size 回填 `reserved`**(讓部位數字一開機就正確,不必等成交慢慢對上)。
`realized` 從 0 起算(歷史 accrual 捨棄,pre-launch 可忽略)。Axiom 30d 歷史直接丟,不寫 importer。

## 7. Rebuild & Integrity

- **`rebuild_snapshot_from_log(account, env)`** — 依 `event_seq` 升序把 `event_log` 折疊過既有
  pure `transition()`(`registry_offers.py:77-177`),重算 `position_state` + `offer_claims`。
  用於:① 修了 derivation bug 後重算;② 整合測試驗證。
- **integrity check** — 斷言 `position_state.last_event_seq == MAX(event_log.event_seq)`,且
  (debug/週期性)`rebuild` 到暫存後與 live snapshot 比對相等。便宜的一致性守門。

這是選 Hybrid 而非純 snapshot 的價值落地:append-only log 帶完整 payload → 隨時能 deterministic 重建。

## 8. Axiom Removal

| 動作 | 位置 |
|---|---|
| 刪 `AxiomClient` / `AxiomConfig` | `axiom.py:74-173` |
| 刪 replay query adapter | `axiom_event_query.py:44-56` |
| 刪 boot 端 Axiom 實例化 + replay 呼叫 | `daemon.py:613-621, 702, 715` |
| `axiom_sink.py` → `diagnostics_sink.py`(寫 `diagnostics` 表);移除 RESERVATION_* 往 Axiom 的 emit | `axiom_sink.py:49-143` |
| 移除 `AXIOM_API_KEY` / `AXIOM_DATASET`(留 `deployment_environment` 給表用) | env / `from_env()` |
| 改寫 T12 整合測試:拿掉 `_poll_until_indexed`,改 Postgres read-your-writes 斷言 | `test_axiom_replay_query_real.py:39-63` |

## 9. Code-Change Map

**新增**
- alembic migration:建 `event_log` / `offer_claims` / `position_state` / `diagnostics`(目前僅單一 baseline,這是第一個增量 migration)。
- `PostgresEventStore`:`append(event, *, txn)` + 查詢 + `rebuild_snapshot_from_log()`。取代 `AxiomReplayQueryAdapter`。
- `diagnostics_sink.py`:decision/safety/audit → `diagnostics` 表。
- structured stdout logger:operational/heartbeat 事件。

**改寫**
- `PaperPositionLedger`(`ledger.py:119-145`):`replay_from_axiom()` → `load_snapshot()`(SELECT position_state)+ 保留 `rebuild_from_log()`;mutation 改在 txn 內寫 event + 更新 snapshot。
- `OfferRegistry`(`registry_offers.py:234-249`):`replay_from_axiom()` → `load_snapshot()`(SELECT offer_claims)。
- 寫入路徑(`reservation_emitting.py:41-65` + executor):導入 A2 —— submit 前 append INTENT、submit 後 append outcome,皆經 store 在 txn 內持久化。
- 開機序列(`daemon.py:599-706`):snapshot load → resolve PENDING → venue reconcile。

**保留不動**
- pure `transition()`(`registry_offers.py:77-177`)—— rebuild 直接重用。
- `fill_tracker` diff 邏輯(`fill_tracker.py:160-200`)—— 開機 reconcile 沿用。
- DomainEventBus(`bus.py`)—— 仍是 in-process fanout;只是 sink 從 Axiom 換成 store + diagnostics。A3 outbox 維持 "Phase 5+" 標記。

## 10. Error Handling

| 情境 | 處理 |
|---|---|
| Postgres 開機不可用 | fail fast + connection retry/backoff(3 次 1/2/4s)。自有 infra、無 eviction,非外部 SaaS 死。 |
| REST submit 失敗 | txn2 寫 `RESERVATION_FAILED`,snapshot 不動,offer_claims → FAILED。 |
| crash 在 INTENT 與 outcome 之間 | 開機 step 3 用 cid 向 venue resolve(CLAIMED 或 FAILED)。 |
| crash 在 txn 中途 | Postgres 原子性:event + snapshot 全進或全不進,無部分狀態。 |
| snapshot / log 偏離 | integrity check 偵測 → `rebuild_snapshot_from_log()` 修復。 |
| venue reconcile 對不上(本地 CLAIMED、venue 無) | 沿用 `fill_tracker` diff:判 filled/cancelled,寫對應事件 + 更新 snapshot。 |
| diagnostics 寫入失敗 | best-effort、降級不阻斷;失敗只記 stdout。 |

**關鍵原則:** SoT 路徑(event_log + snapshot)寫入失敗會阻斷並 surface;diagnostics 路徑失敗
**絕不阻斷交易**。這是 §Q9 分層在錯誤處理上的體現。

## 11. Testing Strategy (TDD)

寫實作前先立測試,尤其前兩個不變式:

- **Unit — projection 折疊**:給定事件序列,`transition()` 折出的 `offer_claims` / `position_state` 正確。
- **Property — snapshot == rebuild**:append N 筆隨機事件 → 斷言 live snapshot 等於 `rebuild_snapshot_from_log()`。
- **Integration — txn 原子性**:模擬 txn 中途失敗 → 斷言 event 與 snapshot 同進同退。
- **Crash-recovery**:INTENT 後、outcome 前注入 crash → 開機用 venue stub resolve PENDING(CLAIMED + FAILED 各一)。
- **Cutover**:空 snapshot + venue stub 有開倉 → 開機 reconcile 認領 orphan、`reserved` 從 venue 回填正確。
- **取代 T12**:Postgres read-your-writes 直接斷言(append 後立即查得到),刪掉 indexing-latency polling。

## 12. Out of Scope / Deferred

- **A3 full transactional outbox**(背景 dispatcher 派送)—— solo 單實例不需,維持 `bus.py` 的 "Phase 5+" 標記。
- **真正的 observability 平台**(自架 Grafana+Loki on k3s,或 SaaS)—— 等實際上線當獨立決策引入。
- **event_log retention / partitioning** —— 永久保留即可,規模到了再議。
- **歷史 realized-accrual 回填** —— 乾淨切換已決,捨棄。

## 13. Plan 3 Refinements (2026-05-24, pre-implementation)

Plan 1 (foundation) + Plan 2 (cutover) 已 ship。寫 Plan 3 前對照現況 code 做 gap 分析,
鎖定 4 個 refinement(皆 pre-launch best-practice 對齊,業界光譜已攤):

1. **Plan 3 拆 3a/3b/3c(expand-contract / Strangler Fig)** — Axiom 移除牽動遠超 §8(L3 smoke
   endpoint、signal_engine、health_monitor、fill_tracker、smoke_runner、g1.py 都掛 Axiom)。
   - **3a** — A2 write-ahead intent + boot resolve PENDING + venue reconcile(本 SoT 寫入正解)
   - **3b** — `diagnostics` 表 + `diagnostics_sink`(forensic 落 PG)
   - **3c** — Axiom 全移除(repoint L3、刪 client/adapter/env/CI/test);依賴 3b 先就位
   - 順序 3a → 3b → 3c;contract(3c)最後,等替代物 shadow-verify 過。

2. **`offer_claims` PK 改 `(account_id, deployment_environment, cid)` 複合** — 對齊 `position_state`
   複合 PK + `event_log` account-scoped index。裸 `cid` 是唯一多租戶破口(cid=blake2b(correlation_id+date)
   不含 account_id)。carry-forward (d) 點名;pre-launch 空表 migration 成本 ~0。
   (附:carry-forward (d) 的「`offer_claims.last_event_seq` 恆 0」屬 by-design — `store.py:134` 註解
    說明 FSM state 才是 claims SoT、high-water mark 由 `position_state` 扛;不改。)

3. **L3 smoke endpoint 改查 PG `event_log`(read-your-writes)** — 取代 Axiom round-trip poll。
   對齊 §11「T12 integration test 改 PG read-your-writes 直接斷言、刪 indexing-latency polling」。
   保留 real-money canary 前的 deploy-time wiring-drift gate,且更簡單可靠。

4. **A2 寫入路徑改「全同步 in-command txn」,退役 Plan 2 的 async `PostgresEventSink`** — SoT 持久化
   收進 command txn(txn1 INTENT 送單前 commit、txn2 outcome 送單後 commit,**never 跨 REST call 持 txn**);
   `bus` 降為純 in-memory projection(ledger/registry)+ diagnostics fanout。
   - 業界光譜:event store append 在 command txn 內(EventStoreDB/Marten/Axon/Greg Young ES 正統)
     + write-ahead intent(Stripe PaymentIntent / idempotency-key)= SoT 正解;async fire-and-forget
     subscriber(Plan 2 現況)是 projection 機制、扛 SoT 是錯位。對齊 §5「append+snapshot 同 txn」
     §10「crash 中途靠 PG 原子性」+ §Q9「relational DB 做 transactional invariants」。
   - Plan 2 sink 本是 cutover 過渡(先讓開機脫離 Axiom);3a 是 expand-contract 的 contract step。
   - **projection 實作 note**:`offer_claims` 投影改 **cid-keyed**(cid 是全生命週期穩定身分,所有 event 都帶):
     INTENT→PENDING(voi=NULL)、CLAIMED→CLAIMED(+voi,upsert 同 cid row)、RELEASED→RELEASED、
     FAILED→FAILED。voi-keyed `transition()` 留給 in-memory `OfferRegistry`(fill-tracking by voi)。
     現況 `store._project_offer_claims` voi-keyed 且 skip voi=NULL,3a 需改寫成 cid-keyed 狀態機。

### 13.1 Signature-mapping findings(2026-05-24,影響 3a 拆法)

寫 plan 前 map 精確簽章,發現兩個 §6 假設背後的隱藏前提 → 3a 再拆成 **3a-write / 3a-recovery**:

5. **boot venue reconcile 依賴尚未建好的 authenticated venue 查詢** — §6「沿用 `fill_tracker` diff 做 boot
   reconcile」有隱藏前提。`RestPollingFillTracker._tick` 打 `GET /v2/auth/r/funding/offers`
   (`fill_tracker.py:117`)用的是 build_daemon 傳入的**裸 `httpx.AsyncClient()`**(`daemon.py:618`,無
   base_url、無簽章),且 `BFX_FILL_TRACKER_ENABLED` 預設 False、從沒對 live 跑過。真正的 HMAC 簽章只在
   `live_executor.sign_request`。→ resolve-PENDING + reconcile 前需先建正規 signed
   `BitfinexREST.get_active_funding_offers()`(sign_request + creds threading),非「直接重用 fill_tracker」。
   **決策**:把 **boot resolve-PENDING + venue reconcile 拆成 `3a-recovery`**(含先建 signed offers-query);
   `3a-write` 只做 A2 寫入路徑(INTENT/FAILED 事件 + 同步持久化 + composite PK)。3a-write 是 strict
   improvement(durable write-ahead log 存在),boot 仍走現有 `from_snapshot`(PENDING rows 被 voi-not-null
   filter 排除,無 regression);recovery 緊接其後補上「resolve PENDING」的安全保證。

6. **cid 必須在送單前可算(thread submit_date)** — cid 現在在 `executor.submit` 內部用
   `generate_cid(correlation_id, submit_date)` 產生(`paper.py:65` / `live_executor.py:186`,SubmittedOrder.cid
   回傳)。A2 要在送單**前**寫 INTENT,middleware 必須先算出**同一個** cid。generate_cid deterministic,但
   `submit_date` 跨午夜會漂(executor 已有 CC2 capture-once)。→ 3a-write 需把 `submit_date` 抽到 middleware
   capture-once 後 thread 進 executor(或 middleware 算 cid 後傳入),確保 INTENT 與 outcome 的 cid 一致。

**3a 後修訂的 Plan 3 順序**:`3a-write`(A2 寫入)→ `3a-recovery`(signed offers-query + boot resolve PENDING +
venue reconcile)→ `3b`(diagnostics)→ `3c`(Axiom 全移除)。
