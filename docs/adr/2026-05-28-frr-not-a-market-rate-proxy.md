---
title: FRR 不是市場利率 proxy — candle_close 為唯一 market_rate_source
date: 2026-05-28
status: active
tags: [bfx-funding-bot, decision, backtest, strategy, bitfinex, frr]
related-commits:
  - 74751a2
  - 369a1ab
---

# FRR 不是市場利率 proxy — candle_close 為唯一 market_rate_source

## Context

回測引擎的 `market_rate_source` 一直掛著 `Literal["candle_close", "frr"]` 兩個選項，但 `"frr"` 從未實作（engine 遇之直接 ValueError）。Pending 把它列為「FRR 單位確認：解出單位前不可通電 → 擋著策略層」。Phase 3a Gate 1 曾試 5 個假設找穩定 `close ~ a·frr` conversion，全 FAIL（per-year slope 擺動，遂 fallback `candle_close` 並 defer Phase 3c）。本決策用實證釐清「單位」這個前提本身就錯，並把兩個量解耦。

## Options Considered

- **A. 解耦（選用）**：確立 `candle_close`=市場利率、FRR 當原生 feature，移除誤導的 `"frr"` 選項 + always-pass 診斷，寫 ADR。
- **B. 縮放 proxy**：用近年比值（~130）把 FRR 線性縮放成市場利率代入 engine。
- **C. 重啟 Phase 3c**：multivariate / 近年限定 window 重找能過門檻的 `close~frr` conversion。

## Decision

- **D1**：`candle_close` 是唯一 `market_rate_source`（per-day decimal 市場利率）；收窄 Literal + config `__post_init__` fail-fast guard。
- **D2**：FRR 是 Bitfinex 的**不同的量**，不做單位轉換；未來 FRR-relative 策略（Phase 3c）把它當原生 normalized feature 消費。
- **D3**：移除 `check_frr_unit_stability`（always-pass zombie 診斷）。

## Rationale

實證（Neon fUSD 2016-2026，83904 funding_stats + 85471 candles）：

| | median | 量級判讀 |
|---|---|---|
| `candle_close` (p2 1h) | 2e-4/day | 0.02%/day ≈ 7.3%/yr，max 0.07/day 對得上 2017/2021 牛市尖峰 → **合理的市場利率** |
| `funding_stats.frr` | 1.07e-6 | 比 close 小 112–362x，**逐年比值單調漂移**（362→112） |

- **D1/D2**：選 candle_close 而非任何 FRR conversion，因為 close/frr 無常數因子（per-second 會大 760x、same-unit/hourly 全否定）；比值非平穩代表兩者是不同的量，**Phase 3a 找不到穩定 conversion 是結構性必然（category error），非調參問題**。**不選 B**：概念錯（FRR 非市場利率）+ 脆弱（比值漂移）+ 多餘（candle_close 已是正確 per-day 市場利率）。**不選 C**：前提即錯，高成本；FRR-relative 策略要的是 FRR 自身序列的 trend/spike，本就不需轉成市場利率。代價：放棄「用 FRR 當第二個市場利率來源」的彈性（實際上從不存在）。
- **D3**：always-pass 的 check 不 gate 任何東西，只製造假信心並腐蝕整個 backfill check suite 的可信度（業界資料品質慣例：移除測已結案假設的 zombie check）。FRR drift 監測屬 Phase 3c 建 feature 時、與 feature co-located、由真實 consumer 驅動的 monitor，不是現在掛 backfill validation。

## Result

- `git log --oneline 74751a2^..369a1ab`（design spec + 實作）。
- 流程：brainstorming → 直接 TDD（4 檔 trivial 改動，跳過 writing-plans ceremony，proportionate）。
- Gate green：730 unit + mypy strict + ruff + 49 integration（0 xfail）。

## Followup

- FRR feature-drift monitor 留待 **Phase 3c** 建 FRR-trend/SpikeDetect 時、與 feature co-located、由真實 consumer 驅動引入（取代已移除的 `check_frr_unit_stability`）。

## Lessons

### Rules

- **R1**：診斷 / check 之間「測已結案假設、結果恆 pass」= zombie，腐蝕 check suite 可信度。`Rule: always-pass 的 check 要嘛升級成真 gate、要嘛移除；想保留訊號就改成 emit-only monitor（無 pass/fail）且要有 consumer。`
- **R2**：交易所「單位確認」型 blocker，先用 raw 資料量級對齊真實年化報酬 sanity check，再決定是否值得回歸建模。`Rule: 拿到曖昧的交易所數值（如 1e-6），先 median + 對照已知真實報酬量級判斷單位，別一頭栽進 close~X 回歸 — 兩個不同的量硬回歸會永遠找不到穩定 slope。`

### Observations

- **O1**：`funding_stats.frr` 比 traded rate 低 100-360x 且比值逐年漂移，強烈暗示 Bitfinex FRR 是不同方法學的量（amount-weighted 而非 marginal），非單位差異。

## Amendment (2026-09-27): FRR 自身的單位

- 本 ADR 否定的是「`close ~ a·frr` 有穩定常數」，不是「FRR 沒有單位」。2026-07-06 實測 `funding_stats.frr × 365` ≈ ticker 的 per-day FRR（誤差 <0.5%，`live_attribution.FRR_ANNUALIZATION`），weekly attribution 的 AlwaysFRR arm 用此換算並須通過 `assert_market_rate_band`。D1／D2 不變：換算後仍是 FRR（amount-weighted 平均），不是市場利率；上方 Rationale「same-unit 全否定」指 frr 與 close 同單位的假設。
- spec 的產業框架補記：FRR 只在兩處被消費——實際送出 FRR-pegged 浮動 offer 的 peg 目標，或 FRR-relative 策略裡的原生 feature；fill 機率與 spread 建模的市場參考一律用成交／邊際利率（≈ `candle_close`）。

## Related

- 來源 spec：`2026-05-28-frr-market-rate-decoupling-design.md`（原文已不在 repo，本 ADR 即紀錄）。實作 `369a1ab`。
