---
title: Profit review — 獲利提升走執行層優先（cancel/reprice + book-aware + 量測自動化），非策略層或資金層
date: 2026-07-06
status: active
tags: [bfx-funding-bot, decision, profit, execution-layer, pricing, measurement]
related-commits:
  - aa4842c
---

# Profit Review — 執行層優先（execution-first）

## Context

2026-07-06 多 agent design review（6 面向 finder × per-finding adversarial verifier，43 agents，34 findings 全過驗證）回答「如何提升獲利」。觸發背景：cap 剛上調約 3.3 倍（`aa4842c`）但 G3 量測停在 05-31 INSUFFICIENT_DATA（4 fills）。核心發現：正確性骨幹紮實，但**執行層 submit-only**——無 cancel/reprice（stale offer 永久卡死資金於 0%，4 面向獨立收斂）、定價對 live book 零感知、無持續 realized APR 量測。Prior state：「rate 不是 lever、最佳 rate = close」（[2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md)）建立在 backtest 對部署策略退化成 100% fill 假設上（G13 未接 matrix，ROADMAP:281）——循環論證，live 摩擦未計價。

## Options Considered

- **A. 策略層優先** — 繼續找訊號 alpha / period 調參
- **B. 執行層優先（選用）** — E1 stale-offer cancel/reprice → E2 book-aware rate clamp（含 taker branch）→ E3 量測自動化（G3 weekly timer + per-cell realized APR + AlwaysFRR benchmark arm）；不動策略訊號
- **C. 資金層優先** — fUSD 入金、繼續加 cap

## Decision

- **D1**：走 B，順序 E1→E2→E3。詳細 phase plan 與測試 gate 在 review 報告（見 Related）；E1 的 policy 選擇見 [2026-07-06-e1-stale-offer-reprice-down-only](2026-07-06-e1-stale-offer-reprice-down-only.md)
- **D2**：明確否決／延後——hidden offers（歷史費率 18% vs 15%，負 EV）；utilization 直接當訊號（EDA 已 KILL）；SKIP FRR parking 直上（skip 是 WFO +15-19% edge 來源，只准 backtest 實驗）；fUSD 入金（等 E3 attribution 數據）
- **D3**：P1 佇列（E1-E3 後）——AP Gate B 平行驗證（fUSD dry-run / shadow）、G13 fill model 接 qualification（horizon 4h→1h + 非退化 assert）、a30 tenor telemetry、offer chunking

## Rationale

- **D1 選 B 而非 A**：EDA funnel 已 4 殺、live MR timing alpha ≈ 0——策略側再投入期望值低；執行層則是被驗證的最大 leak（單 cell 的大部分資金可無限期卡死，最壞每日淨損約為月收益池的 2–6%）。且評估任何策略改動都依賴 E3 的量測——量測壞著時做策略研究是盲飛。外部實務（Coinlend / MarginBot 家族 / instabot42）一致做 book-relative + cancel-replace，B 是業界基線而非新研究。
- **D1 選 B 而非 C**：fUSD「較優」只是相對自身 baseline 的 WFO margin（`matrix.py:128`），絕對收益未證；無 attribution 下搬資金同樣盲飛。代價：資金利用率提升延後。
- **B 內部順序**：E1 先於 E2——只 clamp 不 cancel 修不了已掛死單；cancel 機制（事件、idempotent retry、`live_executor.cancel` "4.4b prework"）已建好只缺決策邏輯。E2 的代價：rate 權威部分從 signal 層下放到 deployment 層 execution clamp，修改文件化 layer boundary——接受之，需同步改 ARCHITECTURE.md §4 + standing_quote docstring。
- **D2**：每項否決都有 verifier 證據（費率差／EDA KILL／WFO edge 歸因），非直覺淘汰。

## Result

- 決策時點零 code；review 34 findings（file:line 證據 + verifier 修正）壓縮進研究報告 2026-07-06-profit-design-review（原文不在本 repo）
- 4 個獨立面向收斂到同一頭號 finding（cancel/reprice 缺失，4× CONFIRMED high）——結論穩健

## Followup

- E1 cancel/reprice sweep（gate：unit 全過 + shadow ≥1 天無 cancel churn）
- E2 book-aware clamp + taker branch（gate：unit 全分支 + live_attribution 量到 execution alpha）
- E3 G3 weekly timer + attribution job + AlwaysFRR arm（gate：每週自動報告 + dashboard 三條線）；政策：cap 再加碼前必須最新 G3 PASS
- P1 佇列見 D3；文件債：ARCHITECTURE.md 仍寫舊 cap / Koyeb / Neon

## Lessons

### Rules

- **R1**：backtest 得出「X 不是 lever」前，先驗證該 backtest 對部署策略的摩擦模型非退化（本例 fill model 對 spread=0 全回 1.0，結論循環）。`Rule: 引用 backtest 結論做設計決策時，附帶檢查其摩擦假設對目標策略是否 degenerate。`

### Observations

- **O1**：多面向獨立 review 收斂到同一 finding（4× cancel/reprice）是結論穩健的強訊號——單一 reviewer 的 top finding 可信度遠低於此。

## Related

- Review 壓縮報告（來源）：研究報告 2026-07-06-profit-design-review（原文不在本 repo；原始 `e016197`）
- FRR-floor 報告：研究報告 2026-07-19-frr-floor-backtest、2026-07-19-frr-floor-backtest-frrfill1（原文不在本 repo；原始 `9ae6e3f`）
- 外部訊號 ingest 報告：研究報告 2026-07-19-external-signals-ingest（原文不在本 repo；原始 `13eb913`）
- E1 決策：[2026-07-06-e1-stale-offer-reprice-down-only](2026-07-06-e1-stale-offer-reprice-down-only.md)
- [2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md) — rate-not-a-lever 前提、AP double-gate（D3 的 Gate B 平行化對象）
- [2026-06-06-signal-eda-funnel](2026-06-06-signal-eda-funnel.md) — D2 utilization 否決依據；P2 外部訊號池要走的同一 funnel
- [2026-05-31-g3-bot-vs-idle-reframe](2026-05-31-g3-bot-vs-idle-reframe.md) — E3 要加 AlwaysFRR arm 的量測框架
- [2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md) — E1/E2 的插入點與 single-writer invariant

## Updates (2026-07-19)

- E2 量測 #1（enforce 後 ~9 天）：15/15 **100% fill**、p50 17m — 未觸發回退，續留 enforce（latency 上升為「不 undercut book」預期代價）。
- P2「SKIP FRR parking 矛盾」backtest 裁決：MR-with-FRR-floor 四 arm、兩個 FRR-fill bound 夾住 0 → **不 promote**；矛盾歸約為未量測的 live FRR fill 行為，等 E3 attribution 數據。報告：研究報告 2026-07-19-frr-floor-backtest（原文不在本 repo；`e65eac0`+`9ae6e3f`）。副產品：FRR 儲存單位全史定案（per-day/365）。

## Amendment (2026-09-27): 壓縮 P2 研究與未入 Decision 的 review 裁決

### Amendment Decision

- **D4（review §4，原未入 ADR）**：venue 風險框架是 idle 的資金和借出的資金放在同一間交易所，所以 SKIP 幾乎不降低 venue 風險，卻放棄了全部收益。cap 填滿後要訂獲利 sweep 政策，避免對單一交易所的曝險無限複利墊高。這是政策方向，尚未實作。
- **D5（P2 SKIP FRR parking，2026-07-19 裁決）**：MR-with-FRR-floor **不 promote**。四臂 calendar WFO（train 3mo / test 1mo，deployed MR 參數固定，p2 cells，15% fee）的結果完全取決於 fill 假設：
  - linear fill α=5（下界）：floor 對 MR 為 fUST −0.145%/mo CI [−0.190, −0.103]、fUSD −0.147 [−0.178, −0.118]；AlwaysFRR 年化只有 1.8%/3.2%，fill 0.19。
  - α=1e-9，也就是 FRR 單一定以 FRR 成交（上界）：floor 對 MR 為 +0.076 [0.032, 0.119] / +0.104 [0.080, 0.131]，但 AlwaysFRR 年化 11.1%/14.4%，勝過所有臂。
  - 兩個 bound 夾住 0，所以判讀的關鍵資料是 FRR 單的實測 fill。後續由 [2026-09-22-frr-baseline-ab-via-sub-account](2026-09-22-frr-baseline-ab-via-sub-account.md) 以真實 A/B 裁決。
  - 附帶確認 FRR 單位：`funding_stats.frr` 存的是 per-day FRR/365；close/(frr×365) 的年中位數從 2016 的 ~0.93 降到 2023–26 的 0.33–0.59，這是市場結構變化，不是單位錯誤（gate 範圍為 [0.1, 10]）。
- **D6（P2 外部訊號池，2026-07-19 落地）**：只 ingest 研究資料，不進 live path。研究用 client 刻意與 live `BitfinexREST` 分開：live 要 fail-fast，backfill 要在 429 時 sleep-retry，兩者語意相反。資料表 `perp_funding_rates`（Bitfinex deriv status 從 2019-08 起、Binance USD-M 8h funding）與 `liquidations`（無 symbol 參數、全 symbol 混流，client 限 1 req/25s）。這些資料尚未跑過 EDA 漏斗。

### Amendment Rationale

- D5 相較直接 promote：WFO 的「skip +15–19% edge」與 live alpha≈0 的矛盾在模型裡重現了，而真實情況落在兩個 bound 之間。在沒有 FRR fill 實測之前 promote，等於把未量測的假設直接接進 money path。代價是 SKIP 期間的資金繼續閒置。
- D6 研究 client 不共用 live client：共用的話，要嘛 live 學會 retry（違反 fail-closed），要嘛 backfill 撞到一次 429 就停。代價是維護兩套 Bitfinex public client。

### Amendment Result（重現方式）

- FRR-floor：`cd backend && uv run python -m scripts.run_frr_floor_backtest`（加 `--fill-alpha 1e-9` 得上界）；`git log --oneline e65eac0^..9ae6e3f`。
- 外部訊號：`uv run python -m scripts.ingest_perp_funding`、`uv run python -m scripts.ingest_liquidations`（resume-aware、冪等 upsert）；`git log --oneline 2ab6ea8^..13eb913`。每週 topup 已排進 weekly-report 鏈（[2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md) D4）。
