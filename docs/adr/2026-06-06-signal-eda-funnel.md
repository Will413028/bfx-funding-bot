---
title: Signal EDA Funnel — EDA-first 信號篩選，4 候選信號全 falsified（null result）
date: 2026-06-06
status: active
tags: [bfx-funding-bot, decision, strategy-research, eda, signal-screening, statistics]
related-commits:
  - "dc8a8d6^..f994018"
---

# Signal EDA Funnel — EDA-first 信號篩選，4 候選信號全 falsified

## Context

策略庫 4 個策略（MeanReversion deployed / RatePercentile / AlwaysMarketRate / AdaptivePeriod）全部 time off 單一信號 `deviation-from-EMA`。ROADMAP Phase 3c 列了幾個從未驗證的信號假設（FRR-trend / SpikeDetect / funding_amount covariate）。專案反覆的失敗模式是 **backtest edge live 蒸發**（band-sweep load-bearing lesson、live G3 MR-alpha≈0）。要在「投入做成完整策略」前，先用 cheap effect-size 篩選擋掉不可靠信號——正如 WeekendPremium 當年被 EDA kill（週末效應 4/6 cell 負）後永久移除。

## Options Considered

**forward-return target（要預測什麼）**
- **A. realized lending yield** — 但 offer rate 鎖在 `close_t`，yield ≈ close_t×H 與信號無關（trivial）
- **B. forward rate change `Δ_H = mean(close[t+1:t+H]) − close_t`（選用）** — 信號價值在指導 gating/period
- **C. timing excess return** — 比較放 vs 不放，更貼策略但與 EDA 解耦目標衝突

**推進方式**
- **A. EDA-first 漏斗（選用）** — cheap 篩 effect-size，過門檻才升級成完整策略
- **B. 挑一個直接做完整 pipeline** — 深度優先，但若 EDA 不成立則白做
- **C. 全做成策略再一起 WFO** — 廣度優先，最費工 + 多 multiple-testing 曝險

**EDA 方法**
- **A. IC rank-correlation funnel（選為主）** + **quintile event-study（選為輔）**
- **B. 純 mini-backtest** — 否決：把「信號有無預測力」和「策略怎麼建構」混在一起，違反解耦

**信號形式**
- **A. rolling/relative（選用）** vs **B. absolute** — FRR 有 per-year 9× slope drift

## Decision

- **D1（spec ①）** = forward-target 選 **B forward rate change `Δ_H`**（H∈{2,7,14,30}d），而非 realized yield
- **D2（spec Context/Goal）** = 走 **EDA-first 漏斗**，產出 GO/KILL 清單，過關才升級
- **D3（spec ②）** = **Spearman IC（primary）+ quintile-spread（secondary）+ block-bootstrap CI/p + BH-FDR + 4-gate GO/KILL**，跨 cell × 2 disjoint regime
- **D4（spec ②）** = 信號一律 **rolling/relative，禁絕對值**

## Rationale

- **D1**：rate 已鎖定，信號的價值不在「賺多少」而在預測利率走向以指導 gating/period。代價：Δ_H 是 noisy proxy（非直接 P&L），但對「信號有無方向性預測力」是對的因變數。
- **D2**：先驗 effect-size 再投入完整實作＝WeekendPremium 教訓。**代價**：多一層 EDA 工具；**回報**：擋掉 false-positive 信號的完整策略開發（engine plumbing + WFO 是大工）。premise check 證 EDA 可純 DB-query+pandas 做（FRR/funding_amount 未接 engine），重工延到 promotion。
- **D3**：IC 給數字、quintile 給形狀（抓非線性）；**block**-bootstrap 而非 plain（保留 autocorr，否則高估顯著）；**disjoint** regime 而非 nested（避免 post-2022 overfit 盲點）；**BH-FDR** 因數十個 test 必有運氣過關者（多重檢定是 WeekendPremium 教訓核心）。門檻 IC≥0.03 是 low-SNR funding 的合理 floor。
- **D4**：absolute FRR 會被 regime drift 污染而非量測真預測力（延續 time-series regression CV pitfall）。

## Result

- 實作 + 整合修正 + enrichment + merge：`git log --oneline dc8a8d6^..f994018`（9-task subagent TDD，每 task spec+quality 雙審，opus final = READY_TO_MERGE）。1217 unit 綠，純 offline（零 live/canary/engine/migration 影響）。
- **NULL RESULT — 4 信號全 KILL**：frr_trend(med IC −0.037)/spike_detect(−0.060) 跨 regime 變號（regime-fragile）；funding_supply(−0.027)/utilization(−0.012) 無 FDR-significant IC（128 tests，10 FDR-sig）。候選池不變、4 信號 falsified。
- opus final review trace live data → null result 判定 **SOUND**（無 look-ahead、resample 後 full variance、FDR 非 over-aggressive、sign-flip 機制真實）。報告：研究報告 2026-06-06-signal-eda-funnel（原文不在本 repo）。

## Followup

- 工具（`modules/backtest/signal_eda.py` + `scripts/run_signal_eda.py`）可重用測未來信號假設（不同 horizon / operationalization）。
- 這 4 個信號方向 falsified；ROADMAP Phase 3c 若要做成策略，需用不同立論並先過此漏斗。

## Lessons

### Rules
- **R1**：EDA-first 的 null result 是一等結論，不強造 champion——`Rule: 篩選類研究先把「全 kill 也是有效輸出」寫進 spec（go/kill + null-result-first-class），避免 thoroughness bias 逼出假冠軍`（延續 fill-sweep / WeekendPremium）。

### Observations
- **O1**：null result（4 信號 regime-fragile / 無 edge）與專案核心發現一致——timing alpha 是 regime-dependent、backtest edge live 蒸發；強化「sizing 放大在 bot-vs-idle、timing 信號當未驗 bonus」的立場。

## Related

- 結果報告（來源）：研究報告 2026-06-06-signal-eda-funnel（per-cell × regime × horizon IC/p/quintile grid；原始 `66a2d4f`；原文不在本 repo）
- 第二輪報告：研究報告 2026-07-19-phase3c-reoperationalization（原始 `c1d88ba`；原文不在本 repo）
- 原 spec / plan（已壓縮）：spec `2026-06-05-signal-eda-funnel-design.md`、plan `2026-06-05-signal-eda-funnel.md`（原文已不在 repo，本 ADR 即紀錄）
- Merge `f994018`（--no-ff，pushed origin）
- 執行教訓：回測信號的 row-based rolling window 必須先確認數據時間粒度（premise-miss：hourly candle 致 window 縮水 24×）
- 方法論淵源：time-series regression CV pitfall（FRR regime drift、時序假設要實證）

## Updates (2026-07-19) — 第二輪重跑與 Phase 3c 結案

- ADR Followup 的「不同 operationalization 重新立論」已執行：`frr_curvature`（二階導數）/ `spike_pctile`（percentile-rank）/ `frr_trend` 45-90d 皆 KILL（第三者為 block-bootstrap block-size 敏感度檢查抓出的偽顯著——原 `block_size=20` 只對 H≤30d 有效，長 horizon 需隨 horizon 放大 block，此檢查已成為漏斗的必要補充）。
- `spike_z` 45-90d 過 4-gate（median IC −0.178、4/4 cell、FDR 顯著、過敏感度檢查）但 **NO-GO 不 promote**（Will 授權裁決）：同公式延伸搜尋的 selection-effect、長 horizon 機械性均值回歸混淆、與部署 cells horizon 錯配、timing-alpha-as-bonus 政策。標記 not-promoted（非 falsified），re-open 條件 = E3/ladder 證明 spike capture 可成交 ∧ G3 證明 timing alpha 存在。**Phase 3c 信號假設軌結案**（兩輪 7 個 operationalization）。報告：研究報告 2026-07-19-phase3c-reoperationalization（`5fef65d`+`c1d88ba`；原文不在本 repo）。
- R1（null result 一等結論）第三次驗證有效：第二輪若無敏感度檢查，`frr_trend_extH` 會以 2/4 cell 假 GO 蒙混過關。

## Amendment (2026-09-27): 第二輪數字、重現方式與訊號軸停止

### Amendment Decision

- **D5（2026-07-19 第二輪的方法規則）**：長 horizon（H>30d）的 block-bootstrap 一律同時跑 `block_size=max(20, h)` 的敏感度版本，兩個版本都過才算 GO。原本的 `block_size=20` 只對 H≤30d 有效。第二輪 160 個 observation 做一次 pooled BH-FDR，52 個 FDR 顯著。`frr_trend_extH` 在 block=20 時 2/4 cells robust，放大 block 後變成 0/4；`spike_z_extH` 兩種 block 都是 4/4（24/24 同號）。
- **D6（2026-09-22 strategy-system review §8）**：**停止 forward-rate 訊號研究**。累計 8 個 operationalization，7 KILL、1 NOT-PROMOTED。新的訊號假設只接受期限、成交、幣種配置這三軸，registry 的 do-not-repeat 規則負責擋重測。

### Amendment Rationale

- D5：forward target 在相鄰 row 之間重疊 (H−1)/H，block 太小會讓 CI 過窄，只在長 horizon 產生偽 GO。相較只跑一種 block size，代價是運算量加倍。
- D6：訊號軸已被自己的漏斗證偽，live MR alpha≈0；FRR 對 p2 close 的溢價（2022 後 1.7–3×）指向期限結構。繼續測 forward-rate 的期望值接近零，而非「還沒找到對的 operationalization」。代價是放棄 `spike_z_extH` 以外的訊號延伸搜尋；re-open 條件仍照 2026-07-19 Updates 的規定。

### Amendment Result（重現方式）

- `cd backend && uv run python -m scripts.run_signal_eda`（純函式在 `modules/backtest/signal_eda.py`）。第二輪的資料先在 VM 用一次性容器對 self-hosted Postgres 做唯讀 SELECT，daily-resample 成 CSV 後在本機跑 funnel＋bootstrap，不長時間占用 DB session。
