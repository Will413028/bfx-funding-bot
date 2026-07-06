# 2026-07-06 Profit Design Review — verified findings + execution plan

> 43-agent 多面向 design review（6 dimension finders × per-finding adversarial verifier + completeness
> critic + 外部實務研究）。34 findings 提出、34 全數通過驗證（CONFIRMED 或 PARTIAL）。
> 本文是壓縮版 source of truth：後續 session 從這裡接手，**不需要重跑 review**。
> 決策紀錄：second-brain `wiki/projects/bfx-funding-bot/decisions/2026-07-06-profit-review-execution-layer-first.md`。

## 0. 核心結論（TL;DR）

正確性骨幹（event-sourced、single-writer、90s reconcile）紮實。獲利最大的洞不在策略訊號，
而在**執行層是 submit-only 半成品**：

1. **無 cancel/reprice**（4 個面向獨立發現、全 CONFIRMED high）— stale offer 永久卡死資金於 0%。
2. **定價對 live book 零感知** — rate = 1h candle close 凍 65min，盲目排隊或白送 spread。
3. **量測斷線** — G3 手動且停在 05-31 INSUFFICIENT_DATA（4 fills），cap 卻拉了 3.3x；
   operator 看不到 per-cell 扣費後 realized APR，也沒有 AlwaysFRR benchmark。

**循環論證警告**：「rate 不是 lever、最佳 rate = candle.close」（ADR 2026-06-04）出自 backtest，
但 backtest 的 fill model 對部署策略（spread=0）退化成 100% 成交（`engine.py:121-127` fallback
linear；G13 empirical model 從未接進 matrix runner，ROADMAP:281-282 自認）。「照 close 掛必成交」
的世界裡 rate 當然不是 lever。E1/E2 是執行層修正、非預測訊號，與 EDA funnel 的 KILL 不衝突
（funnel 殺的是 forward-rate 預測）。

規模感：10k cap、fUST 8-15% APR、Bitfinex 抽 15% 利息 → 月淨收益池約 60–105 USDT。
全 cap 卡死一天淨損 ~1.9–3.5 USDT，且時長無上界（Bitfinex funding offer 不自動過期）。

## 1. Execution Plan（E1 → E2 → E3，順序有依賴）

每個 phase 在新 session 走 superpowers 工作流（brainstorming → writing-plans → TDD →
executing-plans → code review）。本節提供 phase 目標、關鍵證據、設計約束、測試 gate —
是 writing-plans 的輸入，不是替代品。

### E1 — Stale-offer cancel/reprice sweep（最高優先）

> **實作 plan 已寫好**（2026-07-06）：`docs/superpowers/plans/2026-07-06-e1-stale-offer-reprice.md`
> — 5 tasks 含完整 code / TDD 步驟 / rollout runbook，直接用 superpowers:subagent-driven-development 執行。

**目標**：offer 掛著不成交時不再永久卡死資金；quote 變了或過期後，reserved 在 ~90s–N min 內
回到分配池以新 quote 重掛。

**現況與證據**：
- 全 codebase 無生產 cancel 路徑（`grep cancel` 在 `modules/execution/deployment/` 只命中
  asyncio task cancel）；reconciler 只送單（`deployment/reconciler.py:111-248`）。
- Cancel 機制**已建好未接**：`CancelRequested`/`CancelAcknowledged` 事件、idempotent cancel
  retry（`execution/retry.py`）、`live_executor.py:267` cancel（標註 "Phase 4.4b prework"）、
  release 路徑由 ws_dispatcher foc + 90s reconcile 收斂（`live_executor.py:272-273` docstring）。
- `ClaimRecord` 沒有 rate 欄位（`registry_offers.py:60-70`）— 需把 rate + created_at 帶進
  ClaimRecord，或從 BootRecovery 的 venue snapshot 讀（snapshot 本來就有 per-offer rate）。
- Go-era 先例：`strategy_specification.md` §13.3（~:1141-1155）有 stale-offer 處理設計可抄。

**設計約束（verifier 確認）**：
- Sweep 放在 DeploymentReconciler tick 內 → 保持 I-SW single-writer；cancel 失敗 = offer 留在
  book（fail-safe）；釋放的 reserved 重新進 gap、重掛走正常 guard chain（I-AC 不變）。
- **防追砍（anti-chase）**：spike 當下不可追著砍高價單（砍掉本來會成交的 spike 單 = 放棄溢價）。
  predicate 至少要：offer rate 高於該 cell 現行 active quote 超過容忍值 **且** 掛齡 > N 分鐘
  （建議 30-60）；或 originating quote 已 SKIP/過期。加 min-hold + hysteresis 防 churn。

**測試 gate**：
- Unit（TDD）：sweep predicate 各 case（stale-by-age／rate-deviation／quote-SKIP／quote-expired／
  spike 中不砍）；cancel-vs-fill race（idempotent，已成交單砍不動不出錯）；cancel 後 reserved
  釋放 → 下 tick gap 重開（走既有 release 路徑的整合測試）。
- Rollout：先 shadow realm 跑 ≥1 天，確認無 cancel churn（diagnostics CANCEL_AUDIT 頻率合理）；
  再 canary。觀察指標：成交延遲（submit→foc EXECUTED）分佈 before/after。
- 全過才 commit：`cd backend_py && uv run pytest -m "not integration"`。

### E2 — Book-aware rate clamp + taker branch

> **實作 plan 已寫好**（2026-07-06）：`docs/superpowers/plans/2026-07-06-e2-book-aware-clamp.md`
> — 5 tasks 含完整 code / TDD 步驟 / rollout runbook（5-verifier 對 codebase 驗證過），直接用
> superpowers:subagent-driven-development 執行。Plan 對本節的三處刻意強化（E1×E2 sweep ref
> 對齊防自砍、down-clamp max_down floor、taker bid_size/bid_period guard）rationale 見 plan Self-Review。

**目標**：submit 時知道自己在 book 的哪個位置；不再高掛排隊或低掛送 spread；spike 時能立即成交。

**現況與證據**：
- Public REST client 只有 `get_funding_candles` + `get_funding_stats`（`external/bitfinex/rest.py`），
  無 ticker/book endpoint；rate 一路原封不動：`mean_reversion.py:67`（rate=close）→
  `signal_engine.py:175-183` → `reconciler.py:217` → `live_executor.py:216`。
- Go-era S1 BestAsk-Relative Pricing 是當年測出的最高價值修正（`strategy_specification.md:80,982`
  「比 bestAsk 低 1 tick 幾乎肯定成交，高 2% 可能永遠排不到」）— Go 移除時一起消失。

**設計**：
- 每 90s tick 抓 `GET /v2/ticker/fUST`（免認證，回 `[FRR, BID, BID_PERIOD, BID_SIZE, ASK, ...]`），
  一 symbol 一 call，走既有 FundingRateLimiter。
- Clamp（MR quote 仍是 POST/SKIP 閘門 + floor）：
  - overshoot：`min(quote.rate, best_ask − 1 tick)` — 防排隊。
  - undershoot/spike：`max(quote.rate, market)` — 不用 65min 前的 stale rate 賤賣。
  - **taker branch**（critic 貢獻）：若 `BID ≥ quote.rate` → 掛 quote.rate 直接吃單即成交
    （短命 spike 唯一保證成交路徑）。注意 taker fill 繼承 BID_PERIOD（可能 >2d），與 P1
    per-tenor cap 有互動。
  - ticker 抓失敗 → fallback `quote.rate`（維持 fail-closed，行為 = 現狀）。
- **Layer boundary 變更**：rate 權威從 signal 層下放一部分到 deployment 層 execution clamp —
  需同步改 `standing_quote.py` docstring + `ARCHITECTURE.md` §4；live_attribution 的
  candle-close passive baseline 會正確把 clamp 量成 execution alpha（保留此對照）。

**測試 gate**：
- Unit：clamp 函式全分支（overshoot／undershoot／taker／fetch 失敗 fallback／tick 換算）。
- Divergence：確認 `marketfeed/divergence_reporter` 對 clamp 後 rate 的報告語意不誤報。
- Rollout：canary 觀察 live_attribution execution alpha 轉正 + 成交延遲下降。

### E3 — 量測自動化（其他一切決策 gated 在這）

**目標**：把 G3 從手動一次性腳本變成每週自動儀表；operator 隨時看得到 per-cell 扣費後
realized APR vs 兩條 benchmark。

**現況與證據**：
- G3 上次 05-31、verdict INSUFFICIENT_DATA；資料起點 2026-05-27 → 到 07-06 約 5-6 個 weekly
  window（門檻 ≥8，headline verdict 預計 ~08 月才夠，但 MR-alpha 診斷現在就比 1 window 有料）。
- `scripts/run_g3_live_validation.py:4` docstring 還寫 Neon — DB 已是 VM 自托 Postgres，要先修。
- 無任何持續 attribution；benchmark 只有 bot-vs-idle（floor），無 AlwaysFRR arm
  （「贏不了免費的 FRR auto-renew 就是零附加值」— 這要成為 cap 加碼 gating bar）。

**設計**：
- (a) systemd timer 每週跑 G3（照抄 `bfx-pg-backup.timer` pattern），dated report 進
  `docs/research/`。
- (b) 持續 attribution job：重用 `scripts/_g3_loaders.py` + `modules/live_validation` 邏輯，
  從 event_log 算 per-cell 每週 fee-adjusted realized APR，落自己的表，webapi 出一個 endpoint
  餵前端。read-only over event_log — 零 invariant 風險。
- (c) AlwaysFRR benchmark arm 加進 G3 報告與 WFO matrix（fee/utilization-adjusted）。
  注意 `funding_stats.frr` 單位問題（~1e-6，不可直接比 candle close；`live_attribution.py:58`）。
- 政策：**cap 再加碼前必須有最新 G3 PASS**（本次 3000→10000 是在 INSUFFICIENT_DATA 上拉的）。

**測試 gate**：attribution 對已知 event fixture 的 unit tests；webapi endpoint smoke；
done = 每週自動產出報告 + dashboard 能看到三條線（bot / always-close / AlwaysFRR）。

## 2. P1 佇列（E1-E3 之後，走既有 research gate）

- **AP Gate B 平行驗證**：AdaptivePeriod 已鎖參數（p_mid=7/p_long=14, band 0.5/2.0）、
  double-gated 至 ~2026-08-04。可零風險提前：fUSD dry-run AP cells（USD 未入金、balance gate
  硬擋）或 shadow realm 累積 Gate B parity 證據，讓 B3 在 Gate A 一過（~07-22）即 arm。
- **G13 fill model 接 qualification**：ROADMAP:281-282 三步 enablement（注意步驟①寫的是
  Neon，對 VM DB 跑）；同時 `fill_horizon_h` 4h→1h（對齊 65min TTL）+ matrix runner 加
  fill_rate 非退化 assert（全 1.0 → warn/refuse）。
- **a30 tenor 錯配 telemetry**：a30 cell 用 2-30d 聚合 series 定價卻掛 2d 單。先零成本量測
  （event log 已有：a30 vs p2 cell 的 posted-rate vs p2-close spread、成交延遲），確認再修
  （改掛 7-14d 或 gating-only）。Go-era G4 term structure（commit 57d9b8c）是 prior art。
- **Offer chunking**：`allocate_gap` 把 >2×min_fill 的單 cell fill 拆成 ≥153 的塊。
  現規模 noise、入金往 10k 走變 material（7k 單一價位 offer 是 book 單層深度的大分數）。

## 3. P2 — research-gated 實驗（先過 backtest/EDA funnel）

- **SKIP FRR parking**：矛盾未解 — WFO 說 skip 貢獻 +15-19% edge，live G3 診斷 alpha≈0
  （但 INSUFFICIENT_DATA）。E3 數據會裁決。在那之前只做 backtest 變體（MR-with-FRR-floor）。
- **Term-structure spread 訊號**（p30−p2 調節 AP tier）：資料已 backfill ~547k rows，
  但 live p30 ingestion 未跑（canary config 無）；fUST p30 sparse 需 LOCF staleness budget。
- **外部訊號池**：EDA funnel 只殺過 4 個內部訊號；perp funding、清算瀑布（Go-era spec
  :127-140 設計過、重寫時丟棄）從未被同標準證偽。走同一條 FDR-IC funnel，零 live 風險。
- **FRR-delta offer type**：executor 加 offer_type（FRRDELTAVAR）當 degraded-mode 保險。

## 4. 否決 / 非 code 決策

- **Hidden offers：不做** — Bitfinex 對 hidden funding offer 歷史費率 18% vs 15%，3pp 幾乎
  必然吃掉資訊優勢（實作前需再驗現行費率表，但 default 否決）。
- **Utilization 當訊號：不直接復用** — EDA funnel 已 KILL（median IC −0.012）。要復活必須
  換假設（fill probability 而非 rate direction）重新過 funnel。
- **fUSD 入金：延後** — WFO margin 是「相對各幣自身 baseline」（`matrix.py:128`），不證明
  fUSD 絕對收益 > fUST（fUST base rate 歷史常有溢價）。等 E3 attribution 上線後做 50/50 實測。
- **文件債**：ARCHITECTURE.md 仍寫 570 cap / Koyeb / Neon（現況：10k cap / VM / 自托 PG）。
- **Venue 風險框架**：idle-in-wallet 與 lent 同在一間交易所 — SKIP 幾乎不減 venue 風險卻
  放棄 100% 收益；cap 填滿後訂獲利 sweep 政策，防單一交易所曝險無限複利墊高。

## 5. 外部實務對照（佐證 E1/E2 是業界基線）

- 共識 pattern：book-relative 定價（depth-walk 或 FRR-anchored ladder）＋資金拆多單 ladder
  進 ask book＋rate 高於均值時鎖長天期＋**4-60 分鐘 cancel-and-replace 週期**＋保留 spike 預備隊。
- MarginBot（eAndrius）：GapBottom/GapTop volume-depth 定位、`ThirtyDayDailyThreshold` 換 30d、
  HighHold 常駐高價 30d spike 單。instabot42/funding-bot：FRR×multiple ladder、
  `updateIntervalMinutes`（預設 60）全撤重掛。Coinlend：book 分析 + 高於均值放長。
- Bitfinex Lending Pro 已於 2024-08-28 停services — 其 "Dynamic" 模式（漸進降 rate 重掛直到成交）
  即 E1+E2 的合體。

## 6. 完整輸出出處

Review 原始輸出（34 findings 完整 file:line 證據 + verifier notes）為 session-ephemeral
（workflow run `wf_c0879e89-a86`）。本文即壓縮後 source of truth；再深的細節回頭看
`git log` 本檔的 blame 對應 session 或直接重驗 code。
