---
title: G3 — live active-vs-passive P&L validation via rate-spread attribution + anchor reconciliation
date: 2026-05-30
status: active
tags: [bfx-funding-bot, decision, validation, strategy, canary, yield-attribution]
related-commits:
  - "95c4f5a^..6539ee8"
---

# G3 — live active-vs-passive P&L validation

## Context

2026-05-28 OOS 證實 deployed MeanReversion config（ema_span=24/thr=0.5）backtest 有 alpha（+0.06–0.07%/mo active return），但那是 **in-sample to the parameter-selection sweep**（同 2022–2026 歷史）。唯一真正的 out-of-sample 是 **live canary 自己**。放大真錢前要確認 deployed config 在真 fills 上產生預測的 active spread。

撞**同一面牆**（與 #4 L2 loss guard 相同，見 [2026-05-30-balance-aware-cap-gate](2026-05-30-balance-aware-cap-gate.md)）：系統無 realized-interest P&L stream，`OrderFilled.size_usdt` / `ledger._realized` 都是**本金 exposure 非損益**；`fill_rate`（=`foc.rate`）是 event 裡唯一 yield 訊號。

## Options Considered

**live active yield 衡量來源**
- **A. Rate-spread attribution**（`event_log` fills + `funding_stats` FRR）— committed yield = deployed-rate × utilization，用 backtest 自己的 `rate×period` 單位
- **B. NAV-delta**（`ReconcileNavTracker` in-memory）— 真 account-equity 變化含收到的利息 + idle drag
- **C. Funding-ledger ingestion**（`/v2/auth/r/ledgers`，未接）— authoritative 收到利息

**capital normalization**：fixed C = allocation cap / live idle balance time series
**windowing**：single aggregate / monthly / weekly dual-tier
**verdict**：binary pass-fail / four-state + anchor

## Decision

- **D1 — yield 來源**：A 當 **primary gate signal**、B 當 **best-effort truth anchor**、C **deferred**。
- **D2 — normalization**：用 fixed C（allocation cap）正規化兩 arm。
- **D3 — windowing**：dual-tier = since-inception **headline** + **weekly** 分布（統計只在 N≥8 window 出，否則 INSUFFICIENT_DATA）。
- **D4 — verdict**：four-state（PASS / INSUFFICIENT_DATA / FAIL / UNRELIABLE），**attribution for signal + 獨立 anchor reconcile for trust**。
- **D5 — 架構**：新 `live_attribution.py` 純函數（無 I/O）+ reuse `oos_profitability` 不改 + thin script；**on-demand report，不自動化任何 scaling**。

## Rationale

- **D1**：A 與被驗證的 backtest **apples-to-apples**（同 yield model），isolate strategy alpha，純讀 durable data 無需新 infra。不選 B 當 gate：near-idle canary 上 NAV 幾乎不動、且 deposit/withdraw 混淆訊號 → 只當 truth anchor。不選 C：新外部整合，marginal rigor 不改變小額 canary→放大的 sizing 決策，B 已從既有抓的資料提供 anchor。best practice（quant shop）= risk system 算 expected P&L、每日對 broker cash ledger reconcile，divergence 觸發調查而非盲信 → 正是 attribution(signal) + anchor(trust) 雙軌。
- **D2**：live idle balance（`available`）**未持久化成 time series**（只 in-memory），用 mandate size C 是標準 attribution baseline，代價 = utilization 以 cap 為分母（idle drag 自動算進 spread 變負）。
- **D3**：weekly 而非 monthly 因 p2 cell 是 2-day cycle（一週含多個完整 cycle 又能在一個月內累積數 window）；N≥8 gate 容忍 idle canary 的稀疏資料，代價 = verdict 長期會是 INSUFFICIENT_DATA（預期狀態）。
- **D4**：anchor 發散 → UNRELIABLE 而非盲信 attribution；NAV anchor 在 uptime 不足時降為 PASS-with-caveat 而非假裝完整對帳。
- **D5**：純函數可完整單元測（無 DB），reuse 已驗證的 OOS metrics 避免重造；on-demand 而非 always-on monitor — scaling 是 operator 手動決策，G3 不自動下單。

## Result

`git log --oneline 95c4f5a^..6539ee8`（design + plan + TDD 實作 + report renderer + Neon loader + ingest_funding_stats）。958 unit / mypy / ruff 全綠；merge main（純 additive）→ 重部署 canary `9bc58477`。

**第一份 live report = INSUFFICIENT_DATA**（1 weekly window、headline active spread 為正）— 如預期：canary near-idle（資金幾乎全數在既有 credit 中、available 僅零頭），稀疏資料是設計容忍的狀態。**註：初版 headline 用了錯的 passive baseline，已於 2026-05-31 修正，見下方 Update。**

## Update 2026-05-31 — Passive baseline 修正（C1）+ AlwaysMarketRate rename（C6）

**Bug**：Option A（上方 line「event_log fills + funding_stats FRR」）實作時 passive arm 拿 `funding_stats.frr`（live ~1e-6/day）當 baseline，但 frr **不是市場利率代理**（比 `funding_candles.close` 的 per-day 市場利率 ~185x 小）。這直接違反 [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md) 已拍板的 `BacktestConfig.market_rate_source="candle_close"`。後果：headline active spread 是在跟一個近零 baseline 比 → headline 幾乎全是 active arm 本身的 return，虛高數倍。backtest 的 `AlwaysFRRStrategy` 名字誤導（實作 lend at `candle.close`），加深了這個誤用。

**36-agent design review（workflow `wczyd4j0e`）判決**：
- ✅ **C1 真 bug**（the whole bug）：passive frr → `funding_candles.close`（fUST/p2/1h、realm-agnostic 無 filter，與 backtest 一致）。
- ✅ C2/C3 portfolio-level 對；C6 rename 安全。
- ❌ **C4/C5/C7 撤回**（誤判，栽在 bot offer period 與市場 avg_period 的混淆）：active duration 維持 **p2=2**（bot 掛 2-day offer，`MeanReversionStrategy.decide()` 對所有 cell hardcode period_days=2）；`funding_stats.avg_period`（~25 天）是市場 auto-renew 平均，**非 bot credit 持有期**。誤當持有期會捏造 ~12x 利息 + 翻 deployment anchor 成 UNRELIABLE。cell key 的 `a30`／`p2` 只決定餵策略哪個 period_agg 的 candle／stats 序列，**不設 offer 天數**（a30 cell 一樣掛 2 天單）；推算現存 credit 何時到期用 `fill_ts + 2 天`，不是 `+ avg_period`。

**Stage 1 已實作（branch `feat/g3-passive-market-rate-baseline` → ff-merge main `0a543b5..50282cd`，TDD 全程，972 unit/mypy/ruff 綠，push origin）**：
- **C1** `0a543b5`：`FrrPoint→MarketRatePoint`；`attribute_passive` 讀 `.rate`；loader `get_in_range(funding_stats)→get_candles_in_range`；新增 `assert_market_rate_band` 單位守門（mean 須在 [1e-5, 0.05]）。
- **C6** `6041d1d`：`AlwaysFRRStrategy→AlwaysMarketRateStrategy`（檔名+class+name 字串；name 字串不落 DB 已查證）。
- **17-agent code review 強化** `8973ae6`：band guard 改 **graceful degrade**（catch ValueError → `UNRELIABLE` verdict + band 訊息，不再 crash report）；抽 pure `_compute_verdict`；`build_verdict_from_neon` 加 `session_factory` 注入 + seeded sqlite wiring 測試（釘 symbol/timeframe/period_agg 選擇）+ inclusive 邊界測試。band 常數用 live 數據驗證（fUST/p2 近 30 天 mean=1.05e-4，無誤判）。

**結果**：live headline 虛高消除；active 仍小幅勝過真實市場利率 baseline。verdict 維持 INSUFFICIENT_DATA（1 window < 8）— Stage 1 **只改 diagnostic headline、不影響結構性 verdict**（near-idle 1-window 本就出不來）。跑 report：`uv run python -m scripts.run_g3_live_validation`（`-m` 模組形式，直接跑 script 會 `ModuleNotFoundError: scripts`）。

**Decision 修訂**：D1 的 Option A 應讀為「event_log fills + **funding_candles.close 市場利率**」；passive arm 名稱為 **AlwaysMarketRate**（非 AlwaysFRR）。

**Stage 2（defer 到資本部署 / window≥8）**：active arm overlap/budget cap（review 另發現 4 筆 fill 累加金額超過 budget、capital-days 141%）、deployment anchor 與 interest duration decouple。

**Stage 2 結果（2026-05-31，`996bac1`..`b769ae7`）**：加 `clamp_active_window`（sweep-line 同時在貸本金 clamp 到 cap；未超 cap 時與舊公式 bit-exact）。實測 peak concurrent = cap → clamp 為 no-op：超過 budget 的累加值是 4 筆 fill 跨時間**累加**、非同時在貸，headline 從未被灌水。順帶修 `total_capital_days` 單位 bug（舊值單位是「天」≈2，`decide_verdict` 契約要 USDT·days，被 n_windows<8 gate 遮住）。anchors 維持 raw，只有 budget-normalized return 才 clamp。

## Amendment (2026-09-27): Stage 2 concurrency clamp 的設計取捨（補錄自 spec）

### Amendment Decision

- **D6 — 超預算時的分配**：選 **proportional scaling**（任一時刻 open principal `S(t) > C` 時，每筆 open fill 的瞬時貢獻乘 `C/S(t)`）而非 greedy／priority cap（先到先得或按利率挑）。
- **D7 — clamp 邊界**：只 clamp budget-normalized 的 active-vs-passive return 與 `total_capital_days`；`attributed_interest`（NAV anchor）與 `attributed_deployed`（deployment anchor）維持 **raw**。
- **D8 — 窗口切分**：fill 依 `fill_ts` 整筆歸屬所在週窗，sweep 走完整 held-to-term 區間、**不在窗尾截斷**；full-span headline 與 `total_capital_days` 用單一 all-fills bucket（完整 joint clamp）。取代先前「跨窗 fill 在窗尾切開」草稿。
- **D9 — 單位**：`total_capital_days` 改為 USDT·days（去掉 `/ capital`），對齊 `decide_verdict` 契約（`min_capital_days = cap × 7`）；over-deploy 時 report 印一行 honesty caveat（`peak_concurrent > cap` 才出現）。

### Amendment Rationale

- **D6**：proportional 對利率中立、決定性、對每筆 fill 公平；不選 greedy／priority：哪幾筆 fill「存活」是任意選擇，會讓 headline 依排序規則變動。
- **D7**：anchor 的任務是對帳 venue 真相（position_state／NAV 反映實際部署，即使超過 cap）；若 anchor 也 clamp，真實的 over-deploy 會被誤標為 model drift。代價：同一份報告裡 return 與 anchor 用不同本金口徑，需要 over-deploy 診斷行說明差額。
- **D8**：不截斷才能保住 held-to-term 假設，且無 clamp 時與舊式 `Σ(size·rate·duration)` 位元一致（每筆 fill 以整數 ms 累加、最後一次除 `MS_PER_DAY`）。代價：per-window bootstrap-CI 的 bucket 不會 joint clamp 跨週界的同時在貸本金——只在 n_windows ≥ 8 時才有影響的有界近似，屆時再評。
- **D9**：舊單位（天，≈2）被 `n_windows < 8` gate 先擋住而未爆；不修的話 window 足夠時 gate 會以錯單位判斷。

Stage 2 報告（2026-05-31，`fc2a834`）：peak concurrent = cap、`detected=false`、raw = clamped interest，headline bot-vs-idle 與 MR alpha 皆為正，verdict INSUFFICIENT_DATA（1 window）。

## Followup

- **累積 ≥8 weekly window** 才能下 active-vs-passive verdict（first-real-money 放大的前置 gate）；需在 live 持續跑 `ingest_funding_stats` 餵 funding_stats。
- **durable NAV-sample persistence**（deferred）— 同時修 `ReconcileNavTracker` restart-amnesia；v1 為避免擾動 reconcile hot path 不做 migration。
- **funding-ledger ingestion（source C）** deferred；**per-currency fUSD** 待 per-currency allocation 上線（canary fUST-only）。
- held-to-term 假設：matured credit 無 discrete close event，用 configured period 當 duration proxy，early cancel 用 `RESERVATION_RELEASED` 校正，gross mismatch 由 deployment anchor 抓。

## Lessons

### Rules

- **R1**：第二次撞「order_fill = 本金 exposure 非損益」（#4 L2 guard、G3 皆然）。`Rule:` 要 live 損益就走 NAV-based（equity delta）或接獨立 funding ledger，**別再期待從 order_fill/ledger 聚合出 realized P&L**。

### Observations

- **O1**：deployed params 來自同一 2022–2026 sweep（selection bias 持續）；live canary IS the true OOS test，但 sample 極小 → CI 會很寬，report 不可 over-claim。

## Related

- 原 spec/plan：`2026-05-30-g3-live-pnl-tracking-error-design.md`、`2026-05-30-g3-live-pnl-tracking-error.md`（原文已不在 repo，本 ADR 即紀錄）
- Stage 2 spec/plan：`2026-05-31-g3-active-concurrency-clamp-design.md` 與 `2026-05-31-g3-active-concurrency-clamp.md`（實作 `996bac1`..`fc2a834`；原文已不在 repo，本 ADR 即紀錄）
- Live 報告：2026-05-30-g3-live-validation（Stage 1 修正後重產）與 2026-05-31-g3-live-validation（Stage 2＋bot-vs-idle 後）（原文不在本 repo）
- 前置脈絡：[2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)、[2026-05-28-strategy-validation-oos-and-deploy-safety](2026-05-28-strategy-validation-oos-and-deploy-safety.md)（OOS 揭露 canary MR config 惰性）
- NAV anchor 共用 [2026-05-30-balance-aware-cap-gate](2026-05-30-balance-aware-cap-gate.md) 的 `ReconcileNavTracker`（#4）
