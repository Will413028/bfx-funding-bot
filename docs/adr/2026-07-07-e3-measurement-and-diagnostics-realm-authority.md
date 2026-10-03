---
title: E3 measurement automation — per-cell attribution + AlwaysFRR benchmark + diagnostics realm authority（Option B）
date: 2026-07-07
status: active
tags: [bfx-funding-bot, decision, measurement, attribution, event-sourcing, identity]
related-commits:
  - "7efabc0^..215c0ce"
---

# E3 measurement automation + diagnostics realm authority

## Context

Profit review 第三號 finding：量測斷線——G3 live-validation 是手動一次性腳本、停在 05-31 INSUFFICIENT_DATA，且無 per-cell 歸因、無跟「免費 FRR auto-renew」的 benchmark 對照。要把它變成每週自動儀表。prior state：G3 純函式（`live_attribution.py`）+ 手動 `run_g3_live_validation`；`funding_stats.frr` 早被 ADR [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md) 判定非市場利率替身（5 個轉換假設全 FAIL）。部署上線後才暴露一個資料模型 bug（見 D4）。

## Options Considered

**per-cell 身分來源**（ORDER_FILL 只帶 `signal_correlation_id`、無 cell）：
- **A. ORDER_SUBMIT 事件**（帶 cell）：但只進 stdout sink（ephemeral、不 durable）。
- **B. diagnostics `kind='decision'` join（選用）**：唯一 durable 的 scid→cell 對照（PG 表）；best-effort/prunable → join 不到歸 `unattributed`。

**AlwaysFRR benchmark 的 rate 來源**：
- **A. 用 raw `funding_stats.frr`**：但非市場利率（ADR 2026-05-28）。
- **B. `frr × 365`（選用）**：2026-07-06 實測 ≈ ticker per-day FRR，band-guarded。
- **C. 跳過 AlwaysFRR**。

**read model 寫入**：
- **A. merge-upsert**：只能新增/覆蓋、不能縮減。
- **B. delete-then-insert（選用）**：單 transaction 全量重算 replace。

**diagnostics realm 錯標修法（部署後發現，見 Result）**：
- **A. loosen loader**：decision join 拿掉 account_id filter、只靠 scid（UUID）+ deployment_environment。
- **B. fix bot writer（選用）**：sink 對 account_id 權威化 + 歷史 retag。
- **C. data-only**：只 retag 歷史、不改 code。

## Decision

- **D1**：per-cell 歸因走 **diagnostics DECISION join**；join 不到的 fill 進 `unattributed` bucket（一級公民，進表與報告）。
- **D2**：AlwaysFRR benchmark arm 用 **`frr × 365`**（常數 `FRR_ANNUALIZATION`），換算後過 `assert_market_rate_band`；band fail → arm 標 unavailable、不炸主報告。
- **D3**：`attribution_weekly` read model 用 **delete-then-insert**（單 transaction、realm-scoped）。
- **D4（true decision，部署後）**：diagnostics realm 錯標選 **Option B** — `DiagnosticsSink` 對 `account_id` 權威化（`self._account_id`，與既有 `deployment_environment=self._env` 一致）+ 一次性 retag 1344 筆歷史 `default→primary`。

## Rationale

- **D1**：ORDER_SUBMIT 雖帶 cell 但 ephemeral；diagnostics 是唯一 durable 對照。**代價**：diagnostics best-effort/prunable（30-90d）→ 舊 fill 可能 join 不到，故 `unattributed` 必須是一級公民、絕不靜默丟棄。
- **D2**：解掉 2026-05-09 FRR 單位懸案——`frr` 非市場利率（ADR 不變），但 `×365 ≈ ticker per-day FRR`（2026-07-07 VM 實測 0.07% 誤差）恰好是「FRR auto-renew 掛單者實得日利率」序列，正是 benchmark 要的。**代價 / 保險**：任何單位漂移由 `assert_market_rate_band` 炸 loader（標 unavailable）而非靜默出錯報告。
- **D3**：event_log 是 append-only SoT、本表是純 read model → replace 語意讓表永遠 = 「以現在 SoT 重算」。merge-only 不能縮減：diagnostics pruning 把 fill 重歸 unattributed 後，舊 (cell,week) row 會殘留舊 interest（read model 漏）。**代價**：空 rows 會清空該 realm——但 event_log append-only，空讀 = 真無 fill，可接受且 fail loud。**不選 A**：leaky read model。
- **D4**：`emit()` 原用 `event.get('account_id','default')`，真實 decision event 不帶 account_id → 全標 `default`，而 event_log fills 帶 `BFX_ACCOUNT_ID=primary` → per-cell join **0 命中、全 unattributed**。**選 B 而非 A**：A（loosen loader）雖小且 scid 是 UUID 不會誤撞，但等於接受「兩個 writer 對同一邏輯 realm 用不同 source」永久存在；B 是長遠乾淨——sink 該對身分權威（跟 `deployment_environment=self._env` 一致），未來資料自然正確。**不選 C**：只補歷史不修 code，未來新資料又錯。**代價**：B 需重啟真錢 bot（載新 sink）+ 一次性歷史 retag（diagnostics 是 best-effort 表、非主 SoT、可逆）。

## Result

- `git log --oneline 7efabc0^..215c0ce`（E3 tasks 1-7 + fixes + Option B）。
- **真錢 VM 部署完成**：migration `959586482e3b`（`attribution_weekly`）+ GRANT SELECT bfx_webapi + funding_stats seed（fUST 66k/fUSD 84k）+ webapi/frontend 重建 + weekly timer（Mon 04:17 UTC）。per-cell 三線儀表 `/attribution` LIVE。bfx-bot 全程只重啟 1 次（Option B），真錢零風險。
- Option B 修後 `run_weekly_attribution` `unattributed=0`，per-cell 通（fUST_a30 3wk/11 fills、fUST_p2 3wk/2 fills）。AlwaysFRR frr×365 vs ticker 0.07% 誤差。
- **數據訊號**：首週 AlwaysFRR > Bot > Always-close → bot live 實測未贏過免費 FRR auto-renew。

## Followup

- **cap 加碼 gated**：`BFX_ALLOCATION_CAP_USDT` 再加碼前必須最新 G3 verdict = PASS **且** AlwaysFRR spread 非負（本次 cap 上調是在 INSUFFICIENT_DATA 上拉的，此政策防重演）。首週 spread 為負 → 暫不加碼。
- ingest 空表無界回填 + autoheal 誤殺（`--label autoheal=false` workaround；長遠可給 backfill lookback 下限）。
- G3 verdict 需 ≥8 weekly windows；timer 每週累積。

## Lessons

### Rules
- **R1**：per-instance 的身分/realm context（account_id / tenant / env）由建構時注入的 authoritative field 決定，**不從每筆事件 payload `get(..., default)` 取**——silent default 是定時炸彈，且單表看不出、跨表 join 才現形。`Rule: sink 對身分欄位權威化，與既有 deployment_environment=self._env 一致`。
- **R2**：event-sourced read model 若宣稱「全量重算」，寫入必須能**縮減**（delete-then-insert），merge-only 達不到——`Rule: rebuildable read model = 純 SoT 函數，用 realm-scoped replace 不用 upsert`。

## Related

- 原 plan `2026-07-06-e3-measurement-automation.md`（原文已不在 repo，本 ADR 即紀錄）；source of truth：review 報告 §1 E3（研究報告 2026-07-06-profit-design-review（原文不在本 repo））。
- FRR 單位解：frr×365 ≈ ticker per-day FRR；[2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)（frr 非市場利率，不變）。
- 上游 roadmap：[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md)；E2 clamp：[2026-07-06-e2-book-aware-clamp](2026-07-06-e2-book-aware-clamp.md)。
- ARCHITECTURE.md §8 量測自動化 / cap 政策。
