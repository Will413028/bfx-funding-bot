---
title: FRR 單位調查採「文獻先行＋不變性門檻」，門檻未過就不寫換算碼、不通電 frr
date: 2026-05-10
status: active
tags: [bfx-funding-bot, decision, spec-compression, frr, backtest, research]
related-commits:
  - "2bb9938^..c10f74f"
---

# FRR 單位調查：文獻先行＋四條不變性門檻，未過即只交研究紀錄

## Context

Phase 2 回填後的 FRR sanity 檢查失敗：`frr × 86400 / candle.close = 669`，推翻 5/9 的「FRR 是秒利率」推測（見 [2026-05-10-historical-backfill-walk-back-to-earliest](2026-05-10-historical-backfill-walk-back-to-earliest.md)）。回測引擎已預留 `market_rate_source: Literal["candle_close", "frr"]`，但單位不明就無法通電；FRR-trend、SpikeDetect 兩個候選策略的閾值也無從設計。Phase 3a 被切成單一目的的小階段：只回答「FRR 怎麼換成 per-day 利率」，不碰策略矩陣。

**約束**：

- `external` Bitfinex 文件只列 funding stats 欄名，不說 FRR 的單位或公式；Help Center 原文當時 403，只拿得到第二手轉述（「以借款金額加權的平均利率、每小時更新、利率以日為單位報價」）。
- `external` 樣本只有 fUSD＋fUST 兩幣（147,528 對 `(frr, avg_period, close)`，2016–2026，1h p2 point-in-time join）。
- `inherited` 真錢策略會吃這個換算；錯的常數會讓所有 FRR 相關閾值整體偏移。現在仍成立（任何 FRR 相關訊號仍走研究→回測→live）。

## Options Considered

- **基準. 以官方文件／SDK 的欄位定義為準**：[Bitfinex Funding Stats 文件](https://docs.bitfinex.com/reference/rest-public-funding-stats)、官方 `bitfinex-api-py` 的 `FundingStatistic`。
- **A. 單點比值分桶**：抽一筆算比值，落在哪個量級就判哪個單位（Go 時代 `inferFRRUnit`、Phase 2 的檢查 3）。
- **B. 文獻先行＋實證回歸＋不變性門檻（選用）**：Stage 1 查 SDK、ccxt、Help Center（各 30 分鐘上限），有權威公式就只驗證那一個；沒有就對 5 個假設跑 OLS `close = a·predictor + b`，四條門檻全過才寫換算碼。
- **C. 取最佳擬合斜率直接通電**：不管門檻，用 R² 最高假設的斜率當常數，先讓 `frr` 跑起來。

## Decision

- **D1 流程**：選 B。5 個假設：H1 `frr`（已是日利率）、H2 `frr × 86400`、H3 `frr × avg_period`、H4 `frr × avg_period × 86400`、H5 `frr / 365`；腳本用 dict 註冊，可以再加。
- **D2 門檻（四條全過）**：R² > 0.99；逐年斜率 CV < 5%；fUSD／fUST 斜率差 < 5%；重建中位相對誤差 < 5%。多個假設同時通過時，取 R² 最高者，同分取較簡單的（H1 > H3 > H4）。
- **D3 失敗分支是硬規則**：門檻沒過就不 commit `conversion.py`、`FundingCandleWithStats`、引擎 frr 分支；只交研究紀錄，引擎維持 `candle_close`。
- **D4 回填檢查降級**：`check_frr_unit` 改名 `check_frr_unit_stability`，只記錄 raw `r²(close ~ frr)`，永遠回 PASS、不影響 exit code。
- **D5 下游範圍**：Phase 3b 只跑不依賴 FRR 絕對值的 4 個策略（RatePercentile／DynamicPeriod／WeekendPremium／MeanReversion，24 runs）；FRR-trend、SpikeDetect 延到 Phase 3c。

## Rationale

- **D1/D2**：選 B 而非基準，因為三個來源都沒有單位：SDK 與 ccxt 只回原值、沒有換算或註解，Help Center 只有「以日報價」的第二手說法，不足以直接採用。**不選 A**：單點只能排除假設（它確實排除了秒利率），不能確認；而且抽到哪一年會影響結論。門檻特別要求逐年與跨幣穩定，因為單一時間點的比值穩定不代表換算常數跨時期成立。代價：門檻嚴到可能什麼都過不了——結果也確實如此。
- **D3**：**不選 C**，因為最佳假設的 R² 只有 0.22；把錯的常數寫進引擎，會讓後面所有 FRR 閾值都建在錯的基準上，而且很難回頭發現。把失敗分支寫成硬規則，就是為了擋住「進度焦慮下硬 ship」。代價是兩個策略延後。
- **D4**：保留一個不擋 exit code 的健康指標，而不是直接刪除，是想在 FRR 語意改變時留下訊號。後來證明這條 always-pass 檢查只會製造假信心，已由 [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md) D3 移除。

## Result

- `git log --oneline 2bb9938^..c10f74f`：spec、plan、研究紀錄三次 commit、`investigate_frr_unit.py`、`c10f74f`（D4）。
- **Gate 1 FAIL**：

| 假設 | R² | 逐年斜率 CV | 跨幣斜率差 | 中位相對誤差 |
|---|---|---|---|---|
| H1／H2／H5 | 0.2168 | 0.5765 | 0.6366 | 0.3712 |
| H3／H4 | 0.0041 | 1.1084 | 1.2284 | 0.5925 |

- H1／H2／H5 數值完全相同：純縮放的假設在帶截距的 OLS 下無法區分，斜率會吸收常數。H3／H4 更差，`avg_period` 是反向訊號。逐年斜率從 2016 年約 387 降到 2023 年約 42（9 倍）。當時據此推論「Bitfinex 改過 FRR 算法」。
- **後續演變**：[2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md) 把 D3 永久化：FRR 不是市場利率，`"frr"` 選項移除，不再找換算。2026-07-19 用量級對齊定案儲存單位：`frr × 365` ≈ ticker 的 per-day FRR（誤差 < 0.5%）；逐年 `close/(frr×365)` 都在 O(1) 且沒有斷點。9 倍漂移是市場結構（FRR 對 p2 close 的溢價擴大），**不是算法改變**，當時的推論不成立（現行 `backend/ARCHITECTURE.md` §8「FRR 單位」）。
- 重現：`cd backend && uv run python scripts/investigate_frr_unit.py`（輸出 `data/research/frr_hypothesis_results.json`，gitignored；exit 1 = 全部未過）。

## Lessons

### Observations

- **O1**：D2 的門檻只能拒絕「單一常數換算」，無法辨認單位：純縮放假設彼此等價，而兩個不同的量回歸永遠不會有穩定斜率。最後單位是靠量級對齊（對照 ticker FRR 與實際年化）解出來的，這條規則已寫在 [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md) R2。

## Related

- 來源（壓縮自 SDD，目錄有追蹤）：
  - `2026-05-10-phase3a-frr-unit-investigation-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-10-phase3a-frr-unit-investigation.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-10-phase3a-handoff.md`（原文已不在 repo，本 ADR 即紀錄）
  - 研究報告全文（Stage 1 三個來源的原文摘錄、H1–H5 表）：研究報告 2026-05-10-frr-unit-investigation（原文不在本 repo）
- 上游：[2026-05-10-historical-backfill-walk-back-to-earliest](2026-05-10-historical-backfill-walk-back-to-earliest.md)（D5 檢查 3 失敗觸發本調查）
- 下游：[2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)、[2026-05-18-phase3b-walk-forward-over-single-split](2026-05-18-phase3b-walk-forward-over-single-split.md)（Phase 3b）
