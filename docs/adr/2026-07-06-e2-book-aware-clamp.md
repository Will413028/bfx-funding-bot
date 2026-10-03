---
title: E2 book-aware rate clamp — submit 前對齊 live funding book（觀察模式 ship）
date: 2026-07-06
status: "superseded-by: [2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md)"
tags: [bfx-funding-bot, decision, execution, pricing, clamp]
related-commits:
  - "7f41e1c^..fbd1a80"
---

# E2 book-aware rate clamp

## Context

Profit review（`e016197`）第二號 CONFIRMED finding：定價對 live book 零感知。signal 層在 1h candle boundary 出 `StandingQuote.rate`（candle close），deployment reconciler 90s 直接照掛——不知道自己在 funding book 的哪個位置。後果：rate spike 時高掛排隊（overshoot、不成交）；rate 崩時用 65min 前的 stale close 賤賣 spread（undershoot）。prior state：E1（`cf29d3b`..`e50427f`）剛加 stale-offer reprice sweep（reprice-down-only + min-age anti-chase），是本決策的鄰居與依賴。

## Options Considered

**定價感知：**
- **A. 維持 book-blind**：signal quote 直接掛，靠 E1 sweep 事後收斂 stale offer。
- **B. book-aware clamp（選用）**：submit 前抓 public ticker，把 quote rate 對齊 book（taker / undercut / raise）。

**clamp 邊界對稱性：**
- **A. 對稱夾**：up/down 都設界。
- **B. 非對稱（選用）**：up-clamp 無上界、down-clamp 以 `max_down_pct` 為 floor。

**上線姿態：**
- **A. enforce-default**。
- **B. observe-only default（選用）**：`BFX_CLAMP_ENABLED=false` 只 log、submit + sweep byte-identical。

## Decision

- **D1**：加純 policy `clamp_rate`（`book_clamp.py`）+ DeploymentReconciler 每 symbol 每 tick 抓一次 public `/v2/ticker/f{sym}`，套 taker / undercut / raise / floor / fallback 五分支。
- **D2**：**up-clamp 無上界、down-clamp 以 `BFX_CLAMP_MAX_DOWN_PCT`（0.15）為 floor**。
- **D3**：clamp enabled 時 **E1 sweep 的 ref_rate 同步對齊 `max(quote ref, ask−1tick)`**。
- **D4**：ship **observe-only default**；enforce 是後續手動 canary（Task 5）。

## Rationale

- **D1**：taker 分支（`bid≥quote` 且 `bid_size≥amount` 且 `bid_period≤上限`）是唯一保證成交路徑；maker 分支掛 `ask−1tick` 搶隊首。純函式 + reconciler 執行分離，clamp 不回寫 quote / 不碰 ledger（I-SW single-writer 不變）。
- **D2**：**風險不對稱**——up-clamp 最壞＝掛太高不成交，由 E1 sweep 在 min-age 後收斂（閒置有界 ~30-60min、可逆）；down-clamp 最壞＝以爛 rate 成交鎖 2 天（不可逆）。故 up 放手、down 設 floor（>15% 下移是 regime 判斷，屬 signal 層職權、交下個 1h boundary 的 MR gate）。**代價**：up 無上界理論上可掛很高，但成交才有害、不成交只是閒置。
- **D3**：不對齊的代價很實——sustained spike 中 E1 sweep 會把 E2 剛以 `~ask−tick` 掛出的高價單當 stale **自砍**（cancel/repost churn）。`max()` 只會**抬高** ref（更少 cancel、更保守），book 下移不加速砍單（reprice-down 節奏仍由 quote 每小時更新決定，E1 語意不變）。這是 review 未涵蓋、實作時新增的交互設計。
- **D4**：真錢 canary，小額 cap；observe 先驗 clamp 頻率/分支分佈再開 enforce。**代價**：多一次 canary gate 的時間，換零行為風險上線。

## Result

- `git log --oneline 7f41e1c^..fbd1a80`（5 commits：ticker → policy → reconciler 整合 → daemon wiring → FLOOR/FALLBACK observe log 修）。
- `subagent-driven-development` 逐 task，5 個 load-bearing invariant（I-SW / fail-safe / anti-undersell / observe=零差 / max()-only-raises）opus 對抗驗證通過；whole-branch review READY_TO_MERGE。
- shipped observe-only（`BFX_CLAMP_ENABLED` 未設）；真錢 bot 行為 byte-identical。

## Followup

- **E2 Task 5 canary（手動 gate）**：先 E1 `BFX_REPRICE_ENABLED=true` 穩 ≥1 週（up-clamp 風險上界依賴 E1 sweep 收斂），再翻 `BFX_CLAMP_ENABLED=true`。observe log 已含 FLOOR 頻率供 go/no-go。
- enforce 後 `mr_alpha_spread` 語意變「timing + execution 混合」（reported-only、never gating）；單獨量測 execution alpha 是 E3 的事。

## Lessons

### Rules
- **R1**：非對稱風險的 policy 邊界要照「最壞後果可逆性」設，不是照對稱美感——`Rule: 可逆的方向（掛太高→被 sweep 收）放手、不可逆的方向（賤賣鎖倉）設硬 floor`。
- **R2**：兩個獨立 policy（E1 sweep、E2 clamp）作用在同一 venue 狀態時，會有 review 看不到的交互——`Rule: 新 policy 疊上既有 policy 前，明確推演「兩者對同一 offer 的動作會不會互相打架」（此處 sweep 自砍 clamp 剛掛的單）`。

## Related

- 原 plan `2026-07-06-e2-book-aware-clamp.md`（原文已不在 repo，本 ADR 即紀錄）；source of truth：review 報告 §1 E2（研究報告 2026-07-06-profit-design-review（原文不在本 repo））。
- 上游 roadmap 決策：[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md)；E3 量測：[2026-07-07-e3-measurement-and-diagnostics-realm-authority](2026-07-07-e3-measurement-and-diagnostics-realm-authority.md)。
- ARCHITECTURE.md §4 步驟 7c、§9 I-BC。
