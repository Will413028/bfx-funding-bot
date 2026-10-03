---
title: Phase 3b methodology — walk-forward CV over single-split, Sortino over raw return
date: 2026-05-18
status: active
tags: [bfx-funding-bot, decision, backtesting, methodology, walk-forward, sortino, quantitative-finance]
related-commits:
  - "cefcbff^..e2c1ab3"
---

# Phase 3b methodology — walk-forward CV over single-split, Sortino over raw return

## Context

Phase 3a Gate 1 FAIL（5/10）後，FRR 單位 5 hypothesis 全 missed，2🟡 策略推遲 Phase 3c。Phase 3b 限縮為 3🟢 strategies × 6 cells，全用 `market_rate_source="candle_close"` proxy。

**5/17 prior state**（commit `cefcbff`）：原 spec 走 single 70/30 train/test split，外加自設「per-quarter regime drift > 0.30 → halt」pre-flight gate 當保險。

**5/18 觸發點**：EDA 跑 post-2022 candles，全 6 cells drift 1.33-2.66× 都觸發 gate；bonus 發現 weekend funding rate 在 4/6 cells 是負 premium（-0.47% 到 -10.96%），WeekendPremium 假設徹底破產。當下決定 halt + 重設計，同日完成 WFO 版 spec + plan + 實作 + 跑完 OOS。

## Options Considered

### Train/test 切法
- **A. Walk-Forward Optimization (WFO)（選）** — rolling 3-month train + 1-month test + 1-month walk，每窗獨立 sweep + eval
- **B. Single 70/30 train/test split** — 簡單，僅在資料明確 stationary 時可用
- **C. Grid search on all data** — data snooping 嚴重，業餘做法

### Sweep optimization metric
- **A. Sortino on monthly-binned equity（選）** — 只懲罰 downside；income strategy 標準
- **B. Sharpe** — 同懲罰 upside variance；對 rate spike 場景不對
- **C. Calmar** — 適合 CTA 趨勢策略；income strategy max_dd 鑑別力低
- **D. Raw return + fill_rate floor** — sweep 會選出過擬合 noise 的 params

### Regime stability 診斷
- **A. WFO inherently handles，無 pre-flight gate（選）**
- **B. Custom drift gate** — bucket size + threshold 都任意；binary pass/fail 強加在連續訊號上
- **C. ADF / Chow / Bai-Perron** — 學術派；publication 用，對 explore phase overkill

### Param grid design
- **A. Data-informed EDA + percentile-based cutoffs（選）** — 適應 cell-specific 分布
- **B. Expert prior（拍 3-5 合理值）** — 絕對 cutoffs 自帶 regime drift 風險
- **C. Bayesian opt (Optuna)** — 2-3 params/strategy 不值得引入依賴

### WeekendPremium strategy
- **A. Keep in candidate set + EDA drop rule**（5/17 設計）
- **B. Permanently drop（選）** — EDA 證實 4/6 cells weekend effect 為負，假設破產

## Decision

- **D1**：WFO 取代 single-split（原 5/17 spec 整段 train/test methodology 替換）
- **D2**：Sortino on monthly equity sampling 為 sweep metric（取代「raw return + fill_rate floor」）
- **D3**：移除 pre-flight regime gate；WFO 結果一致性本身就是 anti-fragility test
- **D4**：Step 0 EDA 必跑；param grid 用 EDA 結果（percentile cuts / σ multiples）而非絕對值
- **D5**：WeekendPremium 永久從候選池移除（不只該 phase skip）

## Rationale

- **D1 WFO**：drift 1.3-2.7× 證明 post-2022 仍 non-stationary，single-split test portion 落在跟 train 不同 regime 結論不可信。WFO 多窗 OOS 平均才能 capture across-regime robustness。**代價**：~48 windows × 6 cells × 2 strategies × 3-6 variants = ~3.5k backtest（vs single-split ~120），但 < 1 hr 可跑完。**不選 single-split**：drift 證據在手；**不選 grid search**：data snooping 嚴重，OOS 結果無意義。
- **D2 Sortino**：income strategy upside spike 不該被懲罰，downside variance 才是真風險。Sharpe / Calmar 對應 trading / CTA 場景，不對 funding lending。Raw return 在 sweep 場景會選 noise-overfit params，OOS false-negative 真正 work 的 conservative params。**代價**：工程 ~50 行 + 月末 equity sampling 邏輯，但下次任何 quant 產品都能 reuse。
- **D3 移除 drift gate**：我自設的 0.30 threshold + per-quarter bucket 都是任意參數，binary pass/fail 強加在連續 drift 訊號上。**WFO 本身**就是 anti-fragility test — 跨多窗 OOS consistent 證明 robust，inconsistent 就是 data 對 backtest 來說太非平穩，不需要前置 gate 多此一舉。**不選 ADF/Chow**：學術儀式；publication 後動作還是 "use rolling estimator" = WFO。
- **D4 EDA-driven grid**：DynamicPeriod 的絕對 0.0003 cutoff 在 2022 用得好不代表 2025（後來 EDA 證實 fUSD/fUST σ 在 0.31-1.41 之間差 4×）。Percentile-based grid 自適應每 cell 分布。**代價**：Step 0 EDA 工程 ~1 hr，但結果可信度大幅提升。
- **D5 WeekendPremium 永久 drop**：weekend effect 4/6 cells 為負（fUST p30 -10.96%），不是「signal 太薄」是「假設方向錯」。保留在 candidate set 只會浪費未來 sweep run 跟膨脹 multiple-testing 偏差。**不選 keep + drop rule**：drop rule 每次 EDA 都會觸發，每次都浪費一輪 evaluation。

## Result

- 5/17 spec 廢棄 → 5/18 重寫；浪費約 3 hrs 工程成本（spec + plan + 5 commits 的 single-split 設計）
- 5/18 全 7 task 在當日完成 + 跑完 Neon WFO matrix
- **OOS verdict**：RatePercentile 6/6 cells qualify，MeanReversion 5/6 cells qualify（fUST p30 thin data fail）；100% health 全 cells，0 errored windows
- Phase 4 候選池：2 個策略，best cell MeanReversion fUSD×a30（+19.9% margin, 81.6% win rate）
- Phase 3c 改為 OPTIONAL（≥ 1 strategy qualify）
- Commits range：`git log --oneline cefcbff^..e2c1ab3 -- 'docs/' 'backend_py/src/bfx_funding_bot/modules/backtest/'`

## Followup

- **Phase 4 ship 準備**：解凍 12 deferred sub-decisions（worker model / real-money safety flags / observability）
- **Stability validation**：上線前對 winning strategies 跑 rolling param drift 觀察（已記 design risk segment）
- **Phase 3c 啟動條件**：若 Phase 4 ship 後發現 RatePercentile/MeanReversion 還是不夠，才回頭做 FRR 單位解碼 + FRR-trend / SpikeDetect
- **fUST p30 thin data**：long-term 看是否要 backfill 更早資料拉到 38k candles 等級

## Lessons

### Rules

- **R1**: 任何 methodology 決策（演算法選擇 / 統計方法 / 架構 pattern），第一個 response 必須 surface 業界 spectrum + recommend，不等使用者問「best practice？」才補。`Rule: spec/plan/brainstorm 階段，方法論決策必先列業界 spectrum 再 recommend；自我檢查 trigger = 我準備寫「我建議 X」之前停一下問「業界 spectrum 我列了沒？」`
- **R2**: 自己發明的 binary gate（threshold + bucket size 都任意）強加在連續訊號上是 anti-pattern。`Rule: 若想加 pass/fail gate 在連續訊號上，先問「有業界標準 metric 嗎？」沒有就 *看連續訊號本身* 不要切 binary。`
- **R3**: Folklore 假設（這次 WeekendPremium）必須先 EDA 驗證 effect size > 設定門檻才能進 sweep。`Rule: 加任何 strategy 候選前，spec 內必含「假設驗證 EDA + drop rule」段，否則該策略 abort。`

### Observations

- **O1**: 從 Phase 3a「raw CV 在 time-series 不可信」到 Phase 3b「pre-flight gate 在連續訊號上不可信」是同一個 anti-pattern 的延伸 — binary thresholds 強加在 continuous noisy signals 上總會 fail（time-series regression CV pitfall）。
- **O2**: Methodology pivot 證據在手立刻 halt（5/18 EDA gate trigger → 30 分內 brainstorm WFO → 同日 ship）比硬上跑完再 post-hoc rationalize 便宜 ~3 倍工程量。

## Amendment (2026-09-27): qualification rule 與 grid 細節補錄

原 spec 將移出 public repo，補上 D1–D5 沒寫到、但決定了 OOS verdict 的規則：

### Amendment Decision

- **D6 qualification rule（取代 5/17 單次 OOS 規則「≥4/6 cells 贏 baseline + 至少 1 cell margin >5% + health」）**：per (cell, strategy) 須 ≥60% eligible windows 的 OOS `net_monthly_return` 贏同窗 `AlwaysFRR(period=2)`、全窗平均 margin >5% relative、≥80% windows 過 health（`fill_rate≥0.3` 且 `n_trades≥5`，1 個月窗所以比 train 的 `n_trades≥10` 鬆）；per strategy 須 ≥4/6 cells。eligible = sweep 有合格 winner 的窗（`skipped:no_valid_candidate`／`errored` 不進分母）；train 或 test <200 candles 的窗直接不生成。窗口按 UTC 月界對齊。60% 自承是 heuristic，報告保留原始分布供事後敏感度分析，**而非**再加一個 binary gate。
- **D7 EDA 一次、全域**：EDA 不按窗重跑，只決定 grid 結構；全 6 cells ACF(168h) 皆 <0.3（最高 fUSD×a30 0.242）→ RatePercentile lookback 只留 168h（每 cell 3 variants）；MeanReversion 的 `ratio_sigma` 依 cell 注入（close/EMA σ 0.31–1.41，fUST 顯著較寬）。
- **D8**：DynamicPeriod 在 5/17 即砍（與 MeanReversion 同賭 mean reversion，選過濾退化 trade 的較乾淨版本）。
- Sweep 細節：train floor `fill_rate≥0.3`、`n_trades≥10`；Sortino 樣本 <3 個月回 0、無 downside 回 +inf，+inf 子集內以 raw return 決勝。

**已知缺口**：spec 說 stability diagnostics（param drift 次數、rolling OOS Sortino）不 gating 但「高 param drift 擋 ship」，results 報告卻從未產出這兩項；每窗各選 winner 也代表 WFO 沒產出單一可部署參數組——2026-05-28 發現部署的是 WFO 沒驗過的固定組（train/serve skew），見 [2026-05-28-strategy-validation-oos-and-deploy-safety](2026-05-28-strategy-validation-oos-and-deploy-safety.md)。

## Related

- 原 spec（原文已不在 repo，本 ADR 即紀錄）：`docs/superpowers/specs/2026-05-17-phase3b-strategy-matrix-design.md`（single-split，已廢棄但保留 traceability）+ `docs/superpowers/specs/2026-05-18-phase3b-wfo-strategy-matrix-design.md`（WFO，採用）
- EDA gate failure doc：研究報告 2026-05-18-phase3b-eda-gate-failure（原文不在本 repo）（per-cell percentiles／σ／ACF 全表）
- Phase 3b-WFO results doc：研究報告 2026-05-18-phase3b-wfo-results（原文不在本 repo）（per-cell 勝率／margin 全表）
- 原 plan：`2026-05-17-phase3b-strategy-matrix.md`、`2026-05-18-phase3b-wfo-strategy-matrix.md`（原文已不在 repo，本 ADR 即紀錄）
  - 重現：`cd backend && uv run python scripts/eda_phase3b.py`、`scripts/run_phase3b_wfo_matrix.py`（資料窗 post-2022-01-01，原跑於 Neon）
- 後續：[2026-05-18-phase4-staged-rollout-paper-shadow-canary](2026-05-18-phase4-staged-rollout-paper-shadow-canary.md)（候選池接手）、[2026-05-28-strategy-validation-oos-and-deploy-safety](2026-05-28-strategy-validation-oos-and-deploy-safety.md)（部署參數 skew 修正）
- Phase 3a related ADR：（無 — Phase 3a 走 spec + lesson 路徑，未獨立寫 ADR；本 ADR 是第一份 bfx ADR）
