---
title: G3 live gate 從 active-vs-passive reframe 成 bot-vs-idle（主成功指標）
date: 2026-05-31
status: active
tags: [bfx-funding-bot, decision, validation, g3, metrics]
related-commits:
  - "46e5edf^..9cd17c0"
---

# G3 live gate 從 active-vs-passive reframe 成 bot-vs-idle

## Context

G3 live validation（[2026-05-30-g3-live-pnl-tracking-error](2026-05-30-g3-live-pnl-tracking-error.md) 建、Stage 1 修好 passive baseline）用 **active-vs-passive** 當主 verdict：部署的 MeanReversion 擇時是否勝過 AlwaysMarketRate（全預算掛市場利率）。但 OOS（2026-05-28）與 live 兩訊號都指 MR 擇時 alpha ≈ 0 → 現 gate 在累積 ~8 weekly windows 後會判 FAIL、暗示「砍掉 MR」。問題是：產品的 value prop 從來不是 MR 擇時 alpha，而是「把 retail 用戶**閒置**的餘額拿去賺市場 funding 利率」。成功判準應是 bot-vs-idle，不是 MR-vs-AlwaysMarketRate。

## Options Considered

**verdict 語意**
- **A. primary/secondary 分層（選用）**：bot-vs-idle 升主 gate，active-vs-passive(MR alpha) 降為 reported 非 gating 診斷
- **B. 雙 verdict 並列**：兩個獨立 verdict、無單一 headline
- **C. 完全取代**：刪掉 active-vs-passive 只留 bot-vs-idle

**scope**
- **A. live-only（選用）** / **B. live + backtest OOS report 一起 reframe**

**implementation**
- **A. 顯式 `attribute_idle` 零報酬 arm（選用）** / **B. 直接拿 active net_monthly 當 CI 序列** / **C. 純 relabel 不改 gate**

## Decision

- **D1**：bot-vs-idle 升主 PASS/FAIL gate；MR alpha（active−AlwaysMarketRate）降為 reported 非 gating 診斷（`mr_alpha_spread/ci_lo/ci_hi/available`）。
- **D2**：market-rate **coverage guard + band guard 與 primary verdict 解耦** —— 只標 `mr_alpha_available=False` + prepend caveat，不再強制 INSUFFICIENT/UNRELIABLE；只有 attribution anchor 發散仍 → UNRELIABLE。
- **D3**：scope = live-only；backtest OOS report reframe 列為 deferred follow-up。
- **D4**：用顯式 `attribute_idle` 零 arm（idle≡0），複用既有 `paired_active_returns`+`bootstrap_ci` 機制。

## Rationale

- **D1**：量化驗證慣例是「絕對報酬 vs cash floor」與「alpha vs naive benchmark」都報、但宣告何者 primary；此產品的 primary objective 毫無疑問是絕對報酬 vs 閒置。不選 C（刪除）= 丟失「要不要砍/簡化 MR 模組」的訊號；不選 B（雙 verdict 無主）= operator 沒有宣告的成功判準。代價：active.net_monthly≥0 使 PASS 近乎必然——但這正確且有意義（產品可靠勝過閒置），discriminating 結果移到 anchor（抓 attribution bug）與 MR-alpha 診斷。
- **D2**：bot-vs-idle（idle≡0）數學上不需要市場利率資料，故 passive-arm 的資料品質問題不該綁架 primary verdict（correctness 改善）。代價：被污染的市場序列不再硬擋 verdict——但 caveat + MR-alpha "unavailable" 仍讓 operator 看得到。
- **D3**：canary 正朝錯 metric 累積（urgency 在 live）；且 OOS 絕對報酬 `summarize_oos` 已算（bot-vs-idle 今天就可比對，無需新 arm），故 backtest reframe 不急、自成小單元。相較硬塞進這輪 = 稀釋一個可 review 的乾淨單元。
- **D4**：顯式零 arm 對稱、報告可誠實寫「idle arm = 0% by construction」、複用 paired-CI 機制。不選 B（直接算）= 破壞兩-arm 對稱、報告無法乾淨呈現 idle baseline。

## Result

- `git log --oneline 46e5edf^..9cd17c0`（含 4 feat + review nits + docs；ff-merge main + pushed origin）
- 1017 unit / mypy src / ruff 全綠；brainstorm→spec→plan→subagent-driven TDD（每 task spec+quality 雙審）+ 2 輪 workflow review + opus 終審 ready-to-merge。
- **G3 是 offline report（讀 Neon、不在 canary daemon hot path）→ 不需 redeploy**；重跑 live 報告確認生效：headline bot-vs-idle 為正、MR-alpha 為小幅正值（次要）、verdict INSUFFICIENT_DATA（1 window<8）。

## Followup

- **verdict 需累積 ≥8 weekly windows（≈2 個月）+ capital-days≥cap×7** 才出 PASS/FAIL；現 1 window=INSUFFICIENT_DATA。
- ~~backtest OOS report headline 也 reframe 成 bot-vs-idle~~ —— 已完成（2026-05-31，見下方 Amendment）。
- FAIL 分支（bot-vs-idle CI<0）正常運作下幾乎不可達（僅負利率 regime / 實現虧損觸發），保留求完整。

## Lessons

### Rules

- **R1**：當「驗證指標一直接近零」時，先問「我量的是不是對的東西」而非直接判失敗。`Rule: 指標 reframe（換衡量什麼）優先於 threshold 調整——MR-alpha≈0 不等於產品無價值，換成 bot-vs-idle 才對齊 value prop。`

### Observations

- **O1**：近乎必然 PASS 的 gate 仍可有價值——只要 discriminating power 移到別處（此處 = attribution anchor 抓 bug + MR-alpha 次要診斷）。
- **O2**：多 agent review 審「執行中途的 spec/plan」會把「未實作」當缺陷大量誤報——已抽為通用 review 規則（verification discriminating power、code review checklist）。

## Amendment (2026-09-27): D3 backtest OOS reframe 落地與 OOS 證據（補錄自 spec／research）

### Amendment Decision

- **D5 — OOS report reframe（D3 的 deferred 部分）**：`run_oos_profitability.py` 的 TL;DR 以 bot-vs-idle（`summarize_oos` 的絕對月報酬＝idle≡0 下的 edge）為 headline，MR timing alpha（`active_return_summary` 的 median active／IR／win%）降為「secondary diagnostic」段；**JSON 輸出不變**（`_report_to_json` 維持 dataclass 的忠實序列化，framing 只屬於人讀的 markdown）。不選同步改 JSON key：機器資料契約不隨敘事改名，下游（含 repo 外的 strategy scoreboard 腳本）不用跟著遷移。
- **D6 — sizing 依據**：加碼只看 bot-vs-idle（市場利率捕捉）；MR timing alpha 視為 regime-dependent 的 upside，不作 sizing 依據；真錢加碼 gate 仍是 live G3，不是 backtest。

### Amendment Rationale（OOS 證據，linear fill、mean fill = 1.0）

- **Full history**（fUST 2018-12 起 86 窗、fUSD 2016-07 起 115 窗；參數選自 2022–26，故 2016–21 對參數選擇是真 OOS）：bot-vs-idle 年化 fUST 7.70%、fUSD 10.3–10.7%；每月皆正（worst 0.06–0.14%/mo）；deflated-Sharpe 1.0（n_trials=9）。MR alpha +0.85～+1.03%/yr（win 74–80%、IR 0.39–0.65）。
- **Recent 2022–26**（50 窗/cell，對參數選擇 in-sample）：bot-vs-idle 年化 fUST 7.1–7.2%、fUSD 6.7–7.2%（fUSD −3.5～−3.7 pp：缺 2017/2021 fat spike，best-month 4.30%→1.23%）；worst-month 反而升到 0.24–0.27%/mo。MR alpha **沒有變薄** +0.9～+1.2%/yr，IR 與 win% 四個 cell 全升（fUST_a30 IR 0.51→1.01、win 80→86%）。
- **結論**：推翻「近期 regime 使 MR alpha 衰退」假說——變薄的是 bot-vs-idle 的**水準**（市場利率），不是 alpha。live MR alpha 遠低於 backtest ~+0.06%/mo 的落差歸因於量測：(1) live 窗數不足（INSUFFICIENT_DATA）為主因；(2) 100% fill 假設使 live alpha ≤ backtest by construction；(3) 分母框架不同（live 主 gate 是 bot-vs-idle）。代價：D6 等於承認 backtest 的 alpha 數字不能直接用，只能當 upside。
- **Revocation**：live G3 ≥8 窗後 bot-vs-idle CI 下界 < 0，或 live 可量的 fill rate 明顯 < 1 且 bot-vs-idle 水準跌破 recent-window worst month → 重評 sizing 依據。
- 重現：`cd backend && uv run python -m scripts.run_oos_profitability`（預設 `START_MTS=2022-01-01`；full history 是暫時把 `START_MTS` 改成 2016-01-01 的手動 run，未提交）。報告模板的「fUST (a30, p2)」標題與「2022-2026 in-sample」caveat 在 full-history run 中是模板殘留。

## Related

- 原 spec/plan（已 compress，spec `7541f74`、plan `60ac8c3`；原文已不在 repo，本 ADR 即紀錄）
- OOS reframe spec/plan（Amendment D5）：`2026-05-31-g3-oos-bot-vs-idle-reframe-design.md`、`2026-05-31-g3-oos-bot-vs-idle-reframe.md`；實作 `git log --oneline e90b053^..9dc1509`（原文已不在 repo，本 ADR 即紀錄）
- OOS 報告：研究報告 2026-06-02-oos-full-history-profitability、2026-06-03-oos-recent-window（原文不在本 repo）
- Live 報告（bot-vs-idle 後第一份）：2026-05-31-g3-live-validation（原文不在本 repo）
- 前序 ADR [2026-05-30-g3-live-pnl-tracking-error](2026-05-30-g3-live-pnl-tracking-error.md)（本 reframe 演進其框架；passive baseline 修正見 [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)）
- 領域前提 [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)（passive=candle.close 非 frr）+ [2026-05-30-g3-live-pnl-tracking-error](2026-05-30-g3-live-pnl-tracking-error.md)（p2=2 天非 avg_period）
