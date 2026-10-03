---
title: E1 stale-offer reprice 採 reprice-down-only＋min-age anti-chase sweep，而非定時全撤重掛或對稱改價
date: 2026-07-06
status: active
tags: [bfx-funding-bot, decision, execution-layer, pricing, reprice]
related-commits:
  - "cf29d3b^..e50427f"
  - f13d9c1
---

# E1 stale-offer reprice：reprice-down-only＋min-age anti-chase

## Context

2026-07-06 profit review 的頭號 finding（4 個面向獨立 CONFIRMED high）：執行層只送單、沒有 cancel。Bitfinex funding offer 不會自動過期，掛高於市場的 offer 會把資金無限期卡在 0%；以當時 cap 估算，全卡一天的淨損約為月淨收益池的 2–6%。prior state：cancel 機制（`CancelRequested`/`CancelAcknowledged` 事件、idempotent retry、`live_executor.cancel` 4.4b prework）已建好但沒有接上決策邏輯；`ClaimRecord` 沒有 rate，但 `BootRecovery` 的 venue snapshot 有每張 offer 的 rate。上游排序見 [2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md)。

**約束**：

- `external` funding offer 不自動過期；spike 時段的高價單可能正是會成交的溢價單。
- `external` venue REST rate limit：cancel 會和送單搶同一個 limiter。
- `inherited` I-SW single-writer（[2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md)）：只有 `DeploymentReconciler` 能對 venue 送單或撤單。現在仍成立：daemon 仍是單實例，且 ledger/tracker 的收斂依賴這個前提。
- `inherited` signal quote 只在 1h boundary 更新：「現行 quote」本身每小時才變一次。這一點在 E2 之後被部分推翻，見 D4。

## Options Considered

- **基準. 定時全撤重掛**：每 N 分鐘把所有 offer 撤掉，再照現價重掛。來源：profit review §5 的外部實務對照，instabot42/funding-bot `updateIntervalMinutes`（預設 60）全撤重掛；Bitfinex Lending Pro 的 Dynamic 模式（逐步降價重掛直到成交，2024-08-28 停止服務）；MarginBot 家族的 4–60 分鐘 cancel-and-replace 週期。
- **A. 對稱改價**：offer 與 quote 的偏離不論高低，超過容忍值就撤掉重掛。
- **B. reprice-down-only＋min-age＋每 tick 上限（選用）**：只撤「高於現行 quote ×(1+tolerance) 且掛齡 ≥ min_age」的 offer，最超價的優先，每 tick 最多撤 N 張。
- **C. B 再加上 quote 變 SKIP 或過期時主動撤單**：review 原建議的 predicate 之一。

**上線姿態**：enforce-default 或 observe-only default（`BFX_REPRICE_ENABLED=false` 只 log `reprice_would_cancel`）。

## Decision

- **D1 = B**：純 policy `stale_offers()`（`reprice.py`，零 I/O）＋在 `DeploymentReconciler.deploy()` 開頭執行 sweep；venue offers 由 reconcile 的既有 snapshot 經 `ReconcileResult` 傳入，不多打 REST。預設值：tolerance 10%、min_age 1800s、每 tick ≤3 張；參考價＝該 symbol 所有 active POST quote 中最高的 rate；只有嚴格大於門檻才撤。
- **D2 不採 C**：quote SKIP、過期或沒有 active quote 的 symbol 一律不撤。這是刻意偏離 review 建議。
- **D3**：sweep 不直接改 ledger/tracker；釋放由 WS foc 與下一輪 90s reconcile 收斂，下一 tick 以新 quote 重掛。cancel 失敗時 offer 留在 book（fail-safe）；`ExecutorAuthError` 往上拋（daemon exit 78）；sweep 失敗不得擋住後續 gap 部署。
- **D4（2026-09-23 追加）**：參考價可切換（`BFX_REPRICE_REFERENCE`）。`quote` 為預設且是現行設定；`book` 以 `PeriodPricer` 對當下 exact-period book、同期限、該 offer 剩餘金額重新定價，多個 quote 取最高，book 或對應期限的 quote 不可用時不撤。程式碼已落地，live 尚未切換。
- **D5**：ship observe-only default，canary 觀察 ≥24h 再翻 enforce。

## Rationale

- **B 而非基準**：定時全撤重掛不管 offer 是否真的過時，spike 當下也會把會成交的高價單撤掉（放棄溢價），每週期都付 cancel/submit 的 rate-limit 成本與事件量。B 只處理真正卡住的那一側，每日撤單數約等於 rate regime 變動次數。代價：卡住的時間上限要等到下一個 quote 更新加上 min_age（約 30–90 分鐘），比全撤重掛慢。
- **B 而非 A**：低於 quote 的 offer 會自然被市場吃掉，撤掉它只會增加 churn、讓資金空轉一輪。上撤（reprice-up）要等 E2 取得 book 位置才有意義。
- **不選 C**：resting 高價單是免費的 spike option；卡住的風險已由「下一個 POST quote 出現時觸發 reprice-down」界定上限。SKIP 當下主動撤單的 EV 不明確，所以不做。代價：整段 SKIP 期間，高價單可能掛著不成交。
- **min-age 與上限**：quote 每小時才更新，min_age 30 分鐘讓 spike 發生的那一小時不會追著砍；每 tick 上限限制 churn，也限制 cancel 對 limiter 的占用。
- **D4 book 參考價**：E2／08-23 之後，送單價由 book 決定。參考價若仍用 signal quote，市場沒動時也會把 book 定出的合法價位當成 stale 砍掉（2026-09-22 strategy-correctness 第 2 項重現過）。不直接改預設：這是 money-path 行為變更，要先有 `config_regime` 列與 observe 數據才能歸因。
- **D5**：真錢 canary，先看 predicate 是否只抓到真正 stale 的 offer，也就是 quote 沒變時同一張 offer 不會反覆進出候選清單。代價：多一個 canary gate 的時間。

## Result

- `git log --oneline cf29d3b^..e50427f`（policy → CancelPort 與 venue offers 傳遞 → reconciler sweep → daemon wiring＋ARCHITECTURE §4/§9）。D4：`f13d9c1`。
- 2026-07-07 翻 enforce；72h 內 1 次 cancel、0 error（[2026-07-10-strategy-review-e2-enforce-and-chunking-verdict](2026-07-10-strategy-review-e2-enforce-and-chunking-verdict.md) D1 的證據）。registry 狀態為 LIVE ENFORCE，參考價是 quote。
- 現行行為與不變量 I-RP 見 repo `backend/ARCHITECTURE.md` §4 步驟 7b 與 §9。

## Followup

- D4 rollout（「E1 reprice 參考價切 book」）：加 `config_regime` 列 → `live.env` 設 `BFX_REPRICE_REFERENCE=book`，並維持 `BFX_REPRICE_ENABLED=false` 觀察 `reprice_would_cancel` 的 ref_rate → 啟用 → 用 `report_execution_quality` 比較 fill 率與 cancel 頻率。不要和 book WS 修正放在同一個 release。

## Invariants

- sweep 只撤高於參考價超過 tolerance 且夠老的 offer；不撤低於參考價的、不撤 min_age 內的、沒有 active quote（或 book 模式下 book 不可用）時不撤。
- sweep 不寫 ledger/tracker；只經 `DeploymentReconciler` 呼叫 venue。

## Revocation Triggers

- 撤單頻率接近每 tick（churn），或 submit→fill 延遲分佈惡化 → 重調 tolerance/min_age，或重評 B。
- quote 改為亞小時更新（signal 層不再是 1h boundary）→ min_age 的依據失效，重評 D1 的預設值。

## Related

- 來源（Provenance）：plan `2026-07-06-e1-stale-offer-reprice.md`。D4 來源：`2026-09-22-strategy-correctness-fixes.md`（第 2 項）（原文已不在 repo，本 ADR 即紀錄）。
- 上游：[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md)；鄰居：E2 clamp [2026-07-06-e2-book-aware-clamp](2026-07-06-e2-book-aware-clamp.md)（sweep ref 對齊 `max(quote, ask−1tick)` 防自砍，已由 [2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) 取代）。
- ladder 與 sweep 互相打架的分析：[2026-07-10-strategy-review-e2-enforce-and-chunking-verdict](2026-07-10-strategy-review-e2-enforce-and-chunking-verdict.md) D2。
