---
title: Adaptive-Period 策略型態、參數選擇（p_long=14 / band 0.5,2.0）與 deploy double-gating
date: 2026-06-04
status: active
tags: [bfx-funding-bot, decision, strategy, backtest, deployment, adaptive-period]
related-commits:
  - "45f35ab^..2966d5c"
---

# Adaptive-Period 策略型態、參數選擇與 deploy double-gating

## Context

OOS 工作（2026-06-02/03）確立 **bot-vs-idle（市場利率捕捉）是耐久價值、MR 擇時 alpha 是薄而 regime-dependent 的 bonus**。回測 fill model 把最優掛單利率釘死在 `candle.close`（posting above/below market 的 EV 皆 <1，§fill-model 分析），所以**利率不是槓桿**——所有既有策略（MeanReversion/RatePercentile/AlwaysMarketRate）都掛市場利率。剩下兩個 alpha 槓桿是 **gating**（既有策略用）與 **period（鎖定天數）**，而 `period_days=2` 被所有策略 hardcode → period 是唯一未開發的槓桿。本 ADR 記 Phase 3c 新策略的型態選擇 + 兩輪參數 sweep 的決策 + deploy 串接/gating。全程 offline，`cells.canary.yaml` 不動 → 對 live MR canary 零風險。

## Options Considered

**A. 策略型態（alpha 槓桿）**
- A1 gating（既有 MR/RatePercentile：何時放貸 vs 暫停）
- **A2 lock-duration（選用）**：always-on 掛市場利率 + 用 deviation-from-EMA 調 `period_days`（高於趨勢→鎖長吃反轉、at/below→鎖短、spike→鎖最長）
- A3 above-market pricing（fill model 下 net-negative，YAGNI）
- A4 獨立 spike-detector module（spike 可收編成最高 period tier，不需獨立模組）

**B. p_long（最長鎖定天數）**：7 / 14 / 30

**C. band 門檻 (t1,t2)**：8 變體（t1∈{0.5,1.0,1.5} × t2∈{1.5,2.0,2.5}）；single band vs per-cell band

**D. deploy 時機**：現在 arm canary vs double-gated 延後

**E. band sweep 設計**：fill-sensitivity sweep vs band-edges sweep

## Decision

- **D1 = A2**：新 `AdaptivePeriodStrategy`——同 MeanReversion 的 mean-reversion thesis，但**透過 lock-duration 而非 gating** 表達；always-on 永不暫停（最大化 bot-vs-idle）。deviation 訊號與 MR 共用 `(close−EMA)/EMA` + per-cell `close_over_ema_sigma`。
- **D2 = B/p_long=14**：deploy 在曲線的 knee。
- **D3 = C/band (t1=0.5, t2=2.0)**：single band（非 per-cell）。
- **D4 = D/double-gated**：B1（divergence）+ B2（deploy-gate）現在建（repo-only、零 live 影響），**B3（canary cell）只寫 runbook 不 arm**。
- **D5 = E/band-edges sweep**：fill-sensitivity sweep 設計被對抗 review **killed**，改掃 band edges。

## Rationale

- **D1**：period 是唯一未開發槓桿，且 characterization 顯示 bot-vs-idle 在所有 cell 兩窗 AdaptivePeriod > MR > AlwaysMarketRate。選 lock-duration 而非 gating：honor「always-on 捕捉耐久 bot-vs-idle、adaptive period 當 additive bonus」。不選 above-market（fill model 證 net-negative）、不選獨立 spike module（spike = `deviation > band2` 最高 tier，無需另開模組）。**代價**：momentum failure mode（鎖長卻續漲→卡在市場利率下方），靠 mean-reversion prior + p_long cap + 回測量化緩解。
- **D2**：把 edge 拆成 robust *median*（fill-realistic）vs fat-tail *mean*（fill-fragile spike-capture）。p14→p30 只 +~20% median 卻 +~90% mean（fUSD_a30 best-month 14.76%@p30→8.34%@p14）→ p30 多的幾乎全是 live 不會重現的 fat tail。p14 留 80-84% robust median、**砍半 fat-tail + 30d duration/credit tail-risk**、且因 100%-fill 假設最被違反的正是 fat tail → p14 也是更*誠實*的 live 估計。不選 p7（median 砍太多，fUSD 只剩 p30 的 51%）。win% 跨 p_long 持平 71-77%。
- **D3**：sweep 發現 **t1 是 dominant lever**（t1≥1.0 → fat-tail-erratic 或 median 崩到 ~0）；t2 主要控 p14-tail 不太動 median。median-peak 是 (0.5,1.5) 但它 p14-share 最高（0.13-0.21 = 最 fill-fragile 的 14d-lock 暴露）且緊鄰 t1=1.0 懸崖。選 (0.5,2.0)：**砍半 p14-tail（0.075-0.14）換統計上 marginal 的 median 代價**（4 cell 中 2 個 paired-diff CI tie、2 個僅低 ~0.016%/mo）——與 D2 同精神（捨最不可信的 tail 換 robustness）。single band：t1=0.5 跨 4 cell 皆 robust，無真正 per-cell 分歧值得拆。
- **D4**：現在 arm 太早——(a) 被 Gate A 卡 ~2 個月；(b) divergence/fill 設計**受惠於 G3 的 live-fill learnings**（真 EMA drift 多大、真 fill 多 partial → 直接校準 B1 tolerance + 重估 p14 tail 可信度）；(c) 建 live-parity code 卻晾 2 個月易 bitrot。高價值低 bitrot 的決策（選哪個 p_long/band）現在鎖定。**雙閘**：Gate A = live MR G3 verdict ≥8 weekly windows（先用真錢證 live 執行路徑 + bot-vs-idle thesis）+ Gate B = adaptive_period 自己的 shadow/parity（變動 period 是新行為、edge 又 fill-fragile）。
- **D5**：fill-sensitivity sweep 想回答「live≈0 是不是 fill artifact」——但 (a) **循環**（用假設 100%-fill 的 backtest 掃 fill 真實性 = 拿被測對象當輸入、break-even 是同曲線的逆），(b) 真 live 訊號 `offers_avg=0.02`（prod `reconcile_observation`）已證 fill 不是 live≈0 機制。改掃 band edges（真正的離線可決變數）才成立。R2 review 另修 6 個 decision-machinery 缺陷（paired band-vs-band diff test、disjoint windows 2016-21/2022 非 nested、active-series DSR、p14-share tie-break、UPDATE param_grid test+drop span-168、majority≥3-of-4）。

## Result

- commits：`git log --oneline 45f35ab^..2966d5c`（strategy `45f35ab` / p_long sweep `d761cd7` / B1B2 `049c80d` / band sweep merge `2966d5c`）。
- 1179 unit 綠、mypy/ruff clean。p_long=14 + band (0.5,2.0) 已 fold 進 `param_grid_for_cell`（返回單一 deployed candidate、drop span-168 探索 grid）+ guard test。
- **狀態：merged main, UNDEPLOYED**——AdaptivePeriod inert（不在 `cells.canary.yaml`），param locked-but-not-armed。

## Followup

- **B3 canary arm（gate 已於 2026-09-22 Amendment 重設，見下）**：Gate A0（book snapshot period 覆蓋率，read-only，先做）→ Gate A（執行路徑證據）→ Gate B（shadow-p14 連續 7 天 divergence=0）→ bounded canary 一筆 minimum offer（fUST a30 p14 cell `{ema_span:24, t1:0.5, t2:2.0, p_mid:7, p_long:14}`）；count-tests／deploy_gate 經 `build_strategy` dispatch／caps share 的實作項不變。
- **真錢不變式（B3 arm 前核對）**：`period_days` 的 Bitfinex 上下限 [2,120] 只在策略內夾（`_PERIOD_MAX`），`live_executor.build_offer_payload` 不再夾一次，所以策略端的 clamp 是唯一的保護；config 另有 `p_mid`／`p_long` `Field(ge=2, le=120)`。rate 恆等於 close，浮點只影響選哪一檔 tier，不影響金額。
- **B1 divergence period-boundary（live-wiring 時）**：`period_days` 是 EMA（rel-tol 比對）的 step function，tier 邊界微 drift 會翻 period → 假發散（與 G2 EMA 累加器 rel-tol 比對同類）。已實作的 B1 比對 ema/window/config *inputs* 而非 derived period（B2 review 已 land），live arm 時再驗。
- **derive_cells 處理**：adaptive_period tiers 是*手選*非 swept-and-selected，不適 MR distinguish/not-worse 流程；hand-set + 小 parity test（MR-specific `check_against_fixture` 已忽略非 MR cells）。
- 回測 caveat：100%-fill optimism（最被違反處正是被砍的 fat tail）、params in-sample-ish（EDA 2022-26）、14d-lock 平台/信用 tail-duration > 2d（cap-bounded）。

## Lessons

### Rules
- **R1（fat-tail payoff 的 sizing）**：`Rule:` duration/leverage 類旋鈕（period、band 寬）愈大常使回測 headline（annualized mean）愈漂亮，但增量幾乎全來自最 fill-optimistic 的罕見極端月 → 分解 median(robust) vs mean(fat-tail)，部署在邊際效益轉折（knee），把靠尾部撐起的 headline 當未經 live 確認的 bonus、不當 sizing 依據。p14 與 (0.5,2.0) 兩次同手法。
- **R2（驗 premise）**：`Rule:` 開工建一整套 sweep machinery 前，先問「harness 的假設裡有沒有正好是我宣稱要回答的那個變數」——若錨點（成交真實性、live regime）只有實盤能給，backtest sweep 最多探索離線 DOF、不能宣稱解答它（fill-sensitivity sweep 的循環）。

- **R3（2026-09-22 Amendment，gate 不得綁被 gate 者的 live 窗口）**：Gate A 要求 live G3 ≥8 週窗，而週窗只在連續放貸下累積；halt 一起，gate 永不觸發、AP 零進展。`Rule:` 部署 gate 的條件必須能在被 gate 的東西之外獨立累積；條件依賴 live 證據時，寫明它假設的前提（連續放貸）與前提破裂時的替代證據。（已抽為通用規則：backtesting methodology best practice。）

## Review Notes

**2026-08-16 review**（原定 2026-08-04 複查已過，回填結論）：2026-08-05 已做過這個查核——原「遠端排程 2026-08-04 fire」查無實際對應的 cron/排程任務，B3 canary arm 未動工；G3 windows 因交易暫停+帳號遷移雙重延遲，Gate A（live G3 ≥8 windows verdict）更沒達成。下次複查順延至 2026-11-16（3 個月，估計值，實際觸發時機仍取決於 G3 windows 累積進度與帳號遷移後恢復交易的時程，屆時再視進度決定是否需要更早複查）。

**2026-09-22 Amendment**：Gate A 替換（見下段）；複查日維持 2026-11-16，屆時核對 A0 結果與 shadow-p14 7 天報告。

## Amendment（2026-09-22）

### Amendment Decision

- D4 的 **Gate A** 由「live MR G3 verdict ≥8 weekly windows」改為與 MR alpha 脫鉤、可獨立累積的**執行路徑證據**：(a) 現行 money path（PR #15 之後）≥20 筆 acknowledged live fills 且無未解 UNKNOWN；(b) 新帳號至少一份 G3 報告成功產出（不要求 PASS，只要求量測鏈可用）。
- 新增 **Gate A0**（前置）：以 `funding_book_snapshots`（2026-07-19 起 hourly）統計 period ∈ {7, 14, 30} ask level 的出現率與深度；period 7/14 覆蓋率 <50% 則 p_mid／p_long 重議（p_long→30，或改 eligibility 的 period 規則——後者是 money-path 變更需獨立 review）。
- **Gate B** 寫成可驗收數字：`shadow-p14` profile（`deploy/vm/shadow-p14.env` + `configs/cells.experimental-p14.yaml`）連續 7 天 `divergence=0`、errors=0。
- B3 arming 走 Halt 2 bounded canary（一 account、一 symbol、一 cell、一道 command、`canary_command_permit`），fUST a30 p14，一筆 minimum offer。
- D1–D3、D5 與「double-gated、不現在 arm」不變。

### Amendment Rationale

- 原 Gate A 的兩個目的（真錢證明 live 執行路徑、用 live-fill learnings 校準 B1）都在說「執行路徑」，不是「MR alpha」。執行路徑自 06-04 後已由 E1/E2 live enforce、G3 73 fills、PR #11–#15 money path 獨立證明；但 gate 文字綁的是 G3 週窗，週窗要連續放貸才累積。07-27 起 live 大部分時間 halted（candle bug → 帳號遷移 → book WS），gate 成循環死鎖：不放貸→沒窗→AP 永不 arm。
- 不選「維持原 Gate A 等窗口」：等待期間 AP 零進展，且窗口何時累積取決於與 AP 無關的 halt 解除時程。不選「只留 Gate B」：parity 不證明 exact-period eligibility 送得出 14 天單（book 上 period 7/14 level 是否常態存在未知），故加 A0。不選「現在 arm」：A0 未查證，bounded canary 機制才剛落地。
- **代價**：新 Gate A 不再要求 MR bot-vs-idle 先 PASS，AP 可能在 MR 尚未 live 驗證時 arm；以一筆 minimum offer 的 bounded canary 把 tail 上界壓在一筆 min offer × 14 天。

## Amendment（2026-09-27）：研究證據補錄與 A0 證據

### Amendment Decision（09-27 證據補錄）

- **證據補錄（D1–D3 不變）**：characterization（p_long=30、band (0.5,1.5)）bot-vs-idle 年化 full／recent：AP fUST 9.9–10.1／9.4–9.9%、fUSD 14.8–15.4／10.6–11.9%，全部 cell 兩窗 AP > MR（7.7–10.7／6.7–7.2%）> AlwaysMarketRate；worst month 全正。period alpha median ~+1.2～+1.4%/yr（win 68–77%）而 mean +3～+6%/yr（fUSD_a30 best-month 14.76% vs baseline 3.82%）→ 兩層 edge：穩定的 duration edge＋彩券型 spike-capture。p_long sweep（full history，median active %/mo p7→p14→p30）：fUSD_a30 0.057→0.093→0.112、mean 0.127→0.231→0.438；p14 保留 p30 median 的 78–92%。band sweep 程序：Step 1 disjoint pre-2022／2022+ 兩半都進前半且 ≥3/4 cell → 存活 (0.5,1.5)、(0.5,2.0)、(1.0,1.5)；Step 2 砍 fat-tail（(1.0,1.5) mean÷median 21×/12×）；Step 3 配對差 CI：(0.5,1.5) 贏 2 平 2、不輸；Step 4 取 knee (0.5,2.0)。
- **B1 設計原則**：divergence 比 `ema_current`（rel-tol）、`window_filled`、`t1/t2/ratio_sigma`，**不比** derived `period_days`（tier 邊界的 step function 只會製造假發散）；B2 gate 經 `build_strategy(cell)` 驗 p14 candidate 勝過 AlwaysMarketRate。同原則見 [2026-05-31-g2-state-level-divergence-detection](2026-05-31-g2-state-level-divergence-detection.md)。
- **A0 證據（未裁決）**：[2026-09-27-keep-always-2d-over-rate-threshold-tenor](2026-09-27-keep-always-2d-over-rate-threshold-tenor.md) 的 book 審計（fUST，hour-weighted，2026-07-19..09-27）量到 exact-14d ask 只在 **5–10%** 小時可見、7d 42–61%（2026-09 為 42%）。依本 ADR A0 規則（7/14 覆蓋率 <50% → p_mid／p_long 重議），**重議條件已觸發**，但 top-25 視窗被 2d ask 佔滿、「不可見」不等於「不存在」；是否改 p_long 或改 eligibility 的 period 規則留待 Will 決定。
- **早還風險同樣適用 AP**：同一研究的 refinance 情境（2d 跌 ≥20% 借方即還）把所有長天期收益壓到 ≈0；AP 的鎖長邏輯（高於趨勢時鎖長吃反轉）正是借方最會提前還款的情境，回測的持有滿期假設在此最樂觀。

### Amendment Rationale（09-27 證據補錄）

- 研究報告原文不在本 repo（見 Related），原「research 報告留 repo」的依賴不再成立，故把決定 D2/D3 的數字與程序寫回本 ADR（source-evaporation test）。
- A0 只記證據不裁決：A0 規則本身給了兩條路（p_long→30 或改 eligibility 規則，後者是 money-path 變更），而 30d 可見度同樣只有 5–10%，兩條路都沒有明顯勝出，屬 owner 的 trade-off。

### Amendment Followup（09-27）

- ~~（決策）A0 觸發後的處置~~ → 已裁決，見下方 Amendment（2026-09-27 owner）。

### Amendment Decision（2026-09-27 owner：A0 處置）

- **改 eligibility 的 period 規則**，而非 p_long→30（30d exact 可見度同樣只有 5–10%，換天期不解決可見度）或 AP 暫緩（放棄已完成的 characterization）；p_long=14、band (0.5,2.0) 不動。代價：money-path 變更——新規則須先有自己的設計與獨立 review 才能 arm，B3 在此之前維持未 arm。owner-confirmed 2026-09-27。
- **設定不一致（已查證 origin/main）**：Gate B 載具 `configs/cells.experimental-p14.yaml`（shadow-p14）四個 cell 皆 `t2: 1.5`，但 D3 選定並 fold 進 `param_grid_for_cell` 的是 `t2=2.0`；deploy-wiring plan 的 B3 範例 cell 也寫 `t2: 1.5`。Gate B 目前驗的是 runner-up band，arm 前需對齊。

## Related

- 原始 spec/plan（已壓縮）：strategy spec `c58ad4a`、band sweep spec `9661907`；deploy-wiring plan：`2026-06-03-adaptive-period-deploy-wiring.md`（原文已不在 repo，本 ADR 即紀錄）。
- Research 報告（原文不在本 repo）：2026-06-03-adaptive-period-characterization、2026-06-03-adaptive-period-fullhistory、2026-06-03-adaptive-period-recent、2026-06-03-adaptive-period-plong-sweep、2026-06-03-adaptive-period-sweep-p7-fullhistory、2026-06-03-adaptive-period-sweep-p7-recent、2026-06-03-adaptive-period-sweep-p14-fullhistory、2026-06-03-adaptive-period-sweep-p14-recent、2026-06-04-adaptive-period-band-sweep。MR 對照組見 [2026-05-31-g3-bot-vs-idle-reframe](2026-05-31-g3-bot-vs-idle-reframe.md) Amendment。
- Gate A 依賴：[2026-05-30-g3-live-pnl-tracking-error](2026-05-30-g3-live-pnl-tracking-error.md)（live G3 bot-vs-idle verdict）。
- caps 依賴：[2026-06-01-per-currency-allocation-phase-2](2026-06-01-per-currency-allocation-phase-2.md)（per-currency cap/balance gate）。
- divergence 設計同類問題：G2 EMA 累加器 rel-tol 比對（見 [2026-05-31-g2-state-level-divergence-detection](2026-05-31-g2-state-level-divergence-detection.md)）。
- 2026-09-22 Amendment 來源：研究報告 2026-09-22-strategy-system-review §3（原文不在本 repo；Amendment 自含）；同批裁決 [2026-09-22-frr-baseline-ab-via-sub-account](2026-09-22-frr-baseline-ab-via-sub-account.md)。
