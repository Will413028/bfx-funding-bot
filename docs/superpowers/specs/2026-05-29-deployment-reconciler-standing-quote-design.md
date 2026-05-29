# Deployment Reconciler — Target-State Reconciliation toward a Standing Quote

**Date:** 2026-05-29
**Status:** Design (brainstorming output, pending implementation plan)
**Related:** `2026-05-29-credit-aware-reconcile-design.md`（reconcile v2 single-writer 骨幹，本設計的擴充基礎）

---

## 問題

canary 真錢營運中發現兩個耦合的缺陷：

1. **單筆送單金額被 venue 靜默拒單（`10001`）。** `cells.canary.yaml` 設 `reference_amount_usdt=150.0`，但 Bitfinex funding 最低額度以 **USD-equivalent $150** 判定；fUST（USDT）≈ 0.999 USD，150 USDT 折算後 < $150 → venue 回 `10001` 拒單。送單前**沒有任何 minimum guard**（`live_executor.submit` 直接把 `offer_amount_usdt` 當 `amount` 送出），失敗只能事後從 venue body 才知道。

2. **沒有「閒置資金即時重投」機制。** 重新放貸的**唯一觸發點是 Scheduler（1h candle boundary）**；`signal_engine.process_candle()` 在 1h tick 時無條件呼叫 `executor.submit()`（signal→submit 為 1:1 同步鏈）。reconcile（90s）只更新 ledger、不送單。資金一旦釋出（offer 未成交過期、credit 關閉），最長閒置 ~1h 才會被重投。

兩者的共同根因：**「決定要不要放貸（signal）」與「把資金部署出去（deployment）」綁在同一條 1h 同步鏈上**，且部署層沒有獨立的金額守門。

---

## 設計原則：target-state reconciliation toward a standing quote

採做市 / 自動放貸系統的業界標準——把兩層**徹底解耦**：

- **Quote 層（慢，事件驅動於 candle boundary）** — 決定*條款*：要不要放、rate、period。即現有 1h `mean_reversion`。產出一個 per-cell 的 **StandingQuote**（或 SKIP = 無 quote）。
- **Deployment 層（連續 / 對帳驅動，唯一送單者）** — 維持不變式：「所有可用資金（到 `target_exposure` 為止）都應以當前 standing quote 在工作」。資金一釋出就用 standing quote 補單，**不重算 signal**。

**為什麼沿用 standing quote 而非 off-boundary 重算：** rate 是慢變量、閒置機會成本是即時的。off-boundary 重算 signal 既無意義（同一根 candle，signal 幾乎不變）又有害（band 邊緣抖動）。

Deployment 層做成**朝 `target_exposure` 收斂的 reconciler**：

```
desired  = (per-cell standing_quote, target_exposure)
actual   = open_offers + active_credits      (venue 真相，reconcile v2 已算)
action   = 補上 gap（若 standing_quote 非 SKIP 且 gap_fill ≥ venue minimum）
```

這把 reconcile v2 的 single-writer 原則延伸到送單：**reconciler 同時是 ledger 的唯一寫者 與 venue 送單的唯一發起者**，消除 boundary-submit 與 idle-fill 兩條路 racing / 雙送的風險。

---

## 架構

### Quote 層（`signal_engine`，1h boundary）

- `process_candle()` 評估策略後，**只產生 / 更新該 cell 的 StandingQuote**，寫入 StandingQuoteStore。
- **移除 line 185 的 `executor.submit(final, account_ctx)`** — signal 層不再送單。
- DECISION 仍照舊寫 `diagnostics` 表（forensics / 可觀測性不變）。

**StandingQuote**（per-cell，in-memory map，key = `cell_id`）：

| 欄位 | 說明 |
|---|---|
| `cell_id` | 來源 cell |
| `outcome` | `POST` / `SKIP` |
| `rate` | offer rate（POST 時） |
| `period_days` | offer 期數（POST 時） |
| `candle_mts` | 產生此 quote 的 candle boundary（TTL 判定用） |

- 儲存：**in-memory map**，由 signal_engine 寫、reconciler 讀。
- **Cold start**：map 為空，等第一個 boundary 填入（同現況行為，可接受）。重啟後 mid-candle 不重投、等下個 boundary。不做 DB 持久化（YAGNI；diagnostics 已有 forensic 記錄）。

### Deployment 層（`periodic_reconcile._tick`，90s，唯一送單者）

在現有 reconcile v2 完成後（venue 真相 → ledger，`PositionReconciled` 已發佈）**新增 deployment phase**：

1. **算 target 與 gap**
   - `target_exposure = allocation_cap`（靜態 ceiling，見下方 sizing；balance-aware 為 future enhancement）
   - `current_exposure = ledger.current_exposure()`（= `reserved + realized`，venue 真相）
   - `gap = target_exposure - current_exposure`
   - `gap ≤ 0` → 無動作（已滿載）

2. **配置 gap 到各 cell**（greedy global + 70% 集中度上限）
   - 只考慮 `outcome == POST` 且 TTL 未過期的 standing quote。
   - **演算法**：在 active cell（POST + TTL 未過期）間平均分配 gap，每 cell 受 `headroom = 0.70 × target − 已部署(intent)` 上限約束；某 cell 觸頂的溢出再均分給其餘未觸頂的 active cell（迭代至無溢出或全觸頂）。
   - SKIP / 無 quote / TTL 過期的 cell 不參與分配 → 其份額自然流向 active cell（greedy 利用率最大化）。
   - per-cell 已部署量用 **intent ledger** 追蹤（見下節），不依賴 venue credit→cell 歸屬。

3. **每筆候選 offer 過 guards（皆 fail → skip 該筆並記明確 reason）**
   - StandingQuote 為 SKIP / 無 quote → 不部署該 cell。
   - **TTL**：`candle_mts` 不在當根 candle → quote 過期，不部署。
   - **Minimum guard**：`gap_fill_usdt < effective_min_usdt` → skip（杜絕靜默 `10001`）。**v1 用靜態式** `effective_min_usdt = ceil(150 × (1 + 0.02)) = 153`——buffer 同時吸收 USDT de-peg 與精度（假設 USDT ≥ 1−buffer，歷史成立）。**不 fetch USDT/USD 行情**（與 balance fetch 同屬 future venue-call）。de-peg 跌破 0.98 才會誤拒，屆時調高 buffer 即可。
   - **Rate sanity band → v1 deferred**：原意防 quote 過時、市場移動後以偏離價補單。但 **TTL 已把 quote 年齡限制在當根 candle 內**，而同一根 candle 的市場利率依定義即該 candle 的利率 → rate sanity 與 TTL 基本冗餘，且需 best-ask 行情 fetch（同屬 future venue-call）。v1 靠 TTL，rate sanity 列 future。
   - **safety_chain.evaluate**：manual_kill / auth_health / heartbeat / allocation_cap —— safety gate 從 signal 層**移到此處**，維持單一 gate。

4. **通過才 `executor.submit()`**，並更新 intent ledger 的 per-cell 已部署量。

### Per-cell intent ledger

greedy + 70% 需要 per-cell exposure，但 venue credit→cell 歸屬正是 `fcn` mapping 已知踩雷處（見 reconcile v2 spec / live-postsubmit-lifecycle incident）。

**處理方式：** per-cell 部署量由 **bot 自己送出的 offer（CID 已知 cell）+ 自己 emit 的 `OrderFilled`** 這條 intent ledger 追蹤，**不依賴 venue credit 歸屬**。

- venue 全域 reconcile 仍是 **total exposure 與 drift 的真相來源**（cap 判定用全域數字）。
- per-cell intent 只用於**分配決策**（greedy + 70%）。
- **一致性維持（不需 venue→cell 歸屬）**：(a) deployment 送單成功 → `deployed[cell] += gap_fill`；(b) 每次 reconcile 後，已知全域真相 `E_total`，令 `S = Σ deployed`，若 `S > 0` 則 **按比例校正** `deployed[c] *= E_total / S`——offer 過期 / credit 關閉造成的全域下降會等比例反映到各 cell，無需逐筆歸屬。
- **Boot edge**：`S = 0` 但 `E_total > 0`（重啟時已有 credit、尚無 intent）→ 無法歸屬，per-cell `deployed` 維持 0，集中度上限暫失效（best-effort，post-boot 自然恢復）；全域 cap 仍由 venue 真相 + `AllocationCapGuard` 嚴格守住。canary 規模可接受。

---

## Sizing

| 旋鈕 | 值 | 說明 |
|---|---|---|
| `target_exposure` (`BFX_ALLOCATION_CAP_USDT`) | **570** | 靜態 ceiling。錢包 ~$600，留 ~5% ops buffer 給 fee / rounding / de-peg。canary pipeline 已驗證，「首跑 bounded downside」理由已過期。cap 是安全護欄，buffer 是工作 headroom。 |
| `venue_floor_usd` | 150 | Bitfinex funding 最低額度（USD-equivalent）。 |
| `min_offer_buffer_pct` | 0.02 | 吸收 USDT 微 de-peg（歷史 ~0.995–1.001）+ price staleness。 |
| per-cell 集中度上限 | `target × 0.70` | 避免全押單一 cell。 |

**`reference_amount_usdt` 的命運：** 在 reconciler 模型下語意改變——不再是「每 tick 送一筆的固定額」，而退化為 per-cell 的 gap-fill。canary 規模（$600 / 2 cells）下，每 cell 一筆 offer 補到其 share 即可，**不做人工 $150 chunking**（chunking 徒增 order 數與 `10001` 風險）。`cells.canary.yaml` 的 `reference_amount_usdt` 欄位移除或標記 deprecated。

---

## Data flow

```
[1h boundary]  Scheduler ── on_scheduler_tick ──> signal_engine.process_candle
                                                      │
                                                      ▼
                                         StandingQuoteStore[cell] 更新
                                         (POST{rate,period,candle_mts} / SKIP)
                                         (DECISION 照舊寫 diagnostics)
                                              ✗ 不再 executor.submit

[90s]  PeriodicReconcile._tick
         │  ① recovery.run() → venue 快照 → PositionReconciled → ledger (reserved/realized)
         │  ② deployment phase:
         │       target = cap; gap = target - current_exposure
         │       配 gap → cells (greedy + 70%, 讀 StandingQuoteStore + intent ledger)
         │       per offer: TTL / min / rate-sanity / safety_chain
         │       pass → executor.submit() → 更新 intent ledger
         ▼
       Bitfinex
```

---

## Error handling / edge cases

- **StandingQuote = SKIP** → 不部署（正確行為：不往壞市場塞錢，非 bug）。
- **Quote TTL 過期** → 不部署，等下個 boundary 刷新。
- **gap_fill < effective_min_usdt** → skip 並記明確 reason event（不再讓 venue 回 `10001` 才知）。
- **Rate off best-ask 過大** → skip（rate sanity band）。
- **Venue 回「not enough balance」**（cap 設超過實際餘額時）→ log + 該輪不再重試該 cell，下輪 reconcile 重評（cap ≤ 錢包 是 operator 責任，static cap 下不主動 fetch balance）。
- **雙送 / 競態** → single-writer 保證：唯一送單者是 reconciler；intent ledger 記錄 in-flight offer，同一 gap 不重複送。
- **Cold start / 重啟** → standing quote 空，等第一個 boundary（同現況）。

---

## Testing

TDD，單元測試先行（`cd backend_py && uv run pytest -m "not integration"` 全過才 commit）：

- StandingQuoteStore：寫入 / 讀取 / TTL 過期判定。
- Deployment phase 分配：greedy 把 SKIP cell 份額流向 active cell；70% 集中度上限；gap ≤ 0 無動作。
- Minimum guard：邊界值（剛好等於 / 略低於 `effective_min_usdt`）；de-peg price 下動態抬高。
- Rate sanity band：off-market quote 被擋。
- safety_chain 在 deployment phase 被呼叫且 block 生效。
- signal_engine 不再呼叫 executor.submit（回歸測試：確認 submit 移除）。
- Intent ledger：送單後 per-cell 量正確累加；in-flight 不重送。

---

## Out of scope（future enhancements）

- **Balance-aware cap**：目前無 available_balance fetch，`target_exposure` 用靜態 ceiling。之後接 Bitfinex
  `/v2/auth/r/wallets` 動態算 `target = min(ceiling, balance × (1 − buffer))`，自動跟隨入金 / 利息複利。
  已記入 `ROADMAP.md` loose-ends。
- **WS 事件驅動的即時重投**：本設計用 90s poll 重投（~1h → ~90s 已是巨大改善）。要更低延遲可改 WS
  `foc`/`fcn` 事件觸發，但 WS 索引之前踩過雷，先用穩的 poll，之後再評估。
- **Per-currency 獨立 allocation**：現為單一 global pool（兩 cell 同為 fUST，無跨幣別耦合問題）。加 fUSD
  cells 前需先 ship 此功能（見既有 per-currency allocation 設計）。
