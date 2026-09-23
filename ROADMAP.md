# 開發路線圖

> 最後更新：2026-05-29（canary 已入金 ~$450 USDT、實際放貸中（3 筆 fUST credit）；近期真錢 incident 修復鏈見「當前位置」）
> 參考文件：`backend/ARCHITECTURE.md`（runtime 架構）, `frontend_architecture.md`, `strategy_specification.md`
> 各 phase 設計/計畫：`docs/superpowers/specs/` 與 `docs/superpowers/plans/`

> **現行後端為 `backend/`（Python）。** 下方「已完成功能」「DB Schema」「API Endpoints」
> 等大表是 Go 後端 MVP 時代的紀錄（**已封存**，見 AGENTS.md），保留作歷史。
> 當前實際開發軌道見下節。

---

## 現行軌道：Python Rewrite（Phase 2–4）

走 backtest → paper → shadow → canary 的真錢上線軌道。

| Phase | 內容 | 狀態 |
|---|---|---|
| 2 | Data backfill — fUSD/fUST candles + funding_stats（~547k rows）回填 Neon | ✅ 2026-05-10 |
| 3a-recovery | Boot recovery + 依 venue id 的 venue reconcile + event-store write-ahead | ✅ 2026-05-24 |
| 3b | Walk-Forward Optimization 策略矩陣 + diagnostics sink；RatePercentile + MeanReversion qualify（best: MR×fUSD_a30, margin +19.9% / win 81.6%） | ✅ 2026-05-18 |
| 3c | 移除 Axiom，改 PG event store 為 SoT（forensics→`diagnostics`、SoT→`event_log`、operational→stdout、smoke→L3 PG endpoint，−3868 LOC） | ✅ 2026-05-25 |
| 4.1 | Paper / shadow infra — live WS → signal → decision logging + health monitor | ✅ |
| 4.2 | Real-money safety + executor 抽象 — hexagonal Executor ports + SafetyGuardChain（hard + calibrated guards） | ✅ 2026-05-21 |
| 4.3 | LOCF + per-cell staleness budget（sparse p30 cells）；backtest 驗證通過 | ✅（G2 calibration audit 待 ~2026-06-10） |
| 4.4 live infra | Bitfinex live executor + WS dispatcher + OfferRegistry FSM（event-sourcing baseline，env flag 控制，paper-preserving） | ✅ Koyeb HEALTHY |
| 4.4 canary | `BFX_PHASE=canary` 啟用 + all-guards invariant + canary deploy config（`cells.canary.yaml` / `safety.canary.yaml`） | ✅ 已上線、已入金 ~$450 USDT、實際放貸中（fUST，cap $450）|

### 當前位置：canary 已入金、實際放貸中（真錢營運 + incident 修復收斂）

canary 真錢 daemon 上線後（2026-05-25），帳戶於 **2026-05-26 入金 ~$450 USDT**，cap 對齊 $450
（`BFX_ALLOCATION_CAP_USDT=450`）。目前**實際放貸中**：3 筆 active fUST credit ≈ $450 全數借出。
前置全備：WFO backtest ✅ / paper ✅ / shadow ✅ / venue reconcile wire+auth ✅ / PG SoT ✅。

入金後從「空跑」進入真錢營運，首次真錢路徑暴露一連串只有真錢才會觸發的 bug，已逐一修復收斂：

- ✅ **live submit 路徑（2026-05-26, `ae2c59d`）**：首次真錢才驗證的 submit code path——symbol 寫死
  fUSD、`str(float)` 產生科學記號 rate、吞掉 venue 回應 body。
- ✅ **post-submit lifecycle（2026-05-27）**：送單後 fill/release/reconcile 從不收斂 → ledger 卡 cap、
  閒置資本誤判已部署。Root cause = WS `foc` 索引錯 + `fcn` 無法映射 credit→offer + reconcile 只在開機跑。
  修法 = **週期對帳骨幹**（`PeriodicReconcile`，`BFX_RECONCILE_INTERVAL_S=90`，venue = SoT、WS 為可失敗的
  延遲優化）+ WS foc 單一來源 parser。Plan 1+2 已部署。
- ✅ **credit-aware reconcile v2（2026-05-29，Koyeb `76f7cd40`）**：上一版 offers-only reconcile 反向過修——
  把撮合成 credit 的 offer 誤 release → `realized` 少記（venue $450 / bot 認 $300），每小時重試過量放貸
  （`10001`）。修法 = reconcile 收斂到**完整** venue 快照（`reserved=Σoffers`、`realized=Σcredits`），
  並重構成 best-practice：single-writer exposure（recovery 事件路由 OfferRegistry 不走 ledger bus）+
  append-only `reconcile_observation` checkpoint（checkpoint ⊕ event-tail rebuild）+ credit 維度
  `RECONCILE DEGRADED` drift 訊號。部署後驗證：`realized` $300→$450、`10001` 噪音停。
  spec `docs/superpowers/specs/2026-05-29-credit-aware-reconcile-design.md`、
  plan `docs/superpowers/plans/2026-05-29-credit-reconcile-v2-best-practice.md`。
- ✅ **executor liveness（2026-05-26, `fix/executor-liveness-health`）**：空跑時 executor heartbeat 累積
  stale（>360s）→ `/healthz` 503 → daemon 重啟。Root cause：executor/safety_chain 是 reactive（只在 POST
  decision 時 bump），不該當 liveness。修法：移出 liveness，HeartbeatGuard 改 watch `ws`（安靜市場靠 hb
  frame 保持 fresh）。見 `docs/superpowers/plans/2026-05-26-executor-liveness-health-refactor.md`。

- 🔄 **capital 授權讀取無界（2026-09-20，未修，blocker）**：`capital_policy` guard 每次評估都
  `eval_timeout >2.0s` 並 fail closed → **即使解除 halt，每一張單仍會被擋**。Root cause =
  `_read_capital` 每次讀取全量反序列化 3,703 筆 `RESERVATION_INTENT`（掃兩次），`_historical_cycles`
  每次載入整個 event log；成本是 O(全部歷史)，已越過 2 秒預算。量測：guard 執行期間 PG 為
  `idle in transaction/ClientRead`（DB 已完成在等應用）、bot 吃滿單核、payload 僅 1,918 bytes。
  失敗模式的嚴重性在於**無聲**：工作量隨歷史成長，系統某天起變成「全部擋掉」且無任何告警。
  修法 = 把稽核移出授權熱路徑（物化 capital position + prefix-hash 綁定 + 尾端 fold；全量重推移到
  snapshot acceptance / boot recovery / periodic reconcile，不一致就寫 durable halt）。
  plan `docs/superpowers/plans/2026-09-20-capital-authority-bounded-reads.md`。

**已知未收尾（loose ends，非 blocker）：**
- `realized_loss_24h` / `drawdown_from_peak` 兩個 calibrated guard 目前接 `_StubPnLSource`（回傳 0）→
  空轉；真 PnLLedger（aggregates realized P&L from order_fill events）待接。放貸利息恆正、本金有平台
  liquidation 保護，realized loss 在正常運作下幾近不可能，故低優先（真接上後再定 threshold）。
- **sub-account 隔離**（credit-aware reconcile 的 next step）：讓「venue 所有 credit = bot 的」嚴格成立，
  使 `realized=Σcredits` 不變式不依賴「帳戶 bot 專用」假設。
- **balance-aware cap**（deployment reconciler 的 future enhancement）：目前 bot 無 available_balance fetch，
  `target_exposure` 直接用靜態 `BFX_ALLOCATION_CAP_USDT` ceiling（5% buffer 變 operator 維持錢包餘額的隱性責任）。
  之後接 Bitfinex `/v2/auth/r/wallets` 動態算 `target = min(ceiling, balance×(1−buffer))`，自動跟隨入金 / 利息複利。
  見 deployment reconciler spec 的 Out-of-Scope 段。

下一個自動化 gate：**G2 calibration audit**（~2026-06-10，shadow 觀察窗結束後）。
Phase 4 結束 = G3 pass（real-money P&L tracking error vs shadow 在 acceptable range）+ Phase 4 results doc。
Phase 5 = SaaS 多租戶化 + scale-up：解 cap / 多 cells / 多策略；per-tenant 帳號（vault 載入加密 API key，取代現 hardcoded `account_id='default'`）+ 用戶 API/dashboard（JWT RS256 auth、AES-256-GCM API key 加密、billing/訂閱方案、per-user API quota）+ 單體→多機(Redis pub/sub)→微服務(API/Lending/Billing/Notification) scale path。
> 原 Go-era SaaS 平台設計藍本（完整 API/CRUD/多租戶/auth/billing 設計）見 git 歷史 `backend_architecture.md`（移除前最後版本 commit `1992b99`：`git show 1992b99:backend_architecture.md`），供 Phase 5+ 在 Python event-sourced 基礎上重新設計時參考。

---

## 已完成功能（Go backend MVP — 已封存）

> 以下為 Go `backend/` 時代的功能清單，Phase 0 重寫至 Python 後不再開發，保留作歷史紀錄。

| # | 功能 | 涵蓋檔案 | Commit |
|---|------|---------|--------|
| 1 | Database Foundation | `schema/schema.hcl` (users, api_keys, user_configs) + Atlas migration | `4b30880` 之前 |
| 2 | Backend API Foundation | Gin + fx + zap, `cmd/server/main.go`, router, health check | 同上 |
| 3 | Bitfinex REST Client | `bitfinex/client.go` + `auth.go` — HMAC-SHA384 認證, funding 操作 | `4b30880` |
| 4 | User Auth JWT | `auth/jwt.go` + `handler/auth.go` + `middleware/jwt.go` — 註冊/登入/JWT 驗證 | `9e08a79` 之前 |
| 5 | AES-256-GCM Crypto | `crypto/aes.go` — API Key 加密儲存 | 同上 |
| 6 | API Key Management | `handler/apikey.go` + `service/apikey.go` + `repository/postgres/apikey.go` — CRUD + Bitfinex 驗證 | `9e08a79` |
| 7 | Strategy Config | `handler/config.go` + `service/config.go` + `repository/postgres/config.go` — CRUD | `9e08a79` 之前 |
| 8 | Lending Engine MVP | `engine/engine.go` + `strategy.go` — ticker loop (3min) + fixed-interval 策略 | `494c794` |
| 9 | Dashboard API | `handler/dashboard.go` + `service/dashboard.go` — GET /dashboard（wallet + offers + credits + MarketSnapshot from Redis） | `760d11b` |
| 10 | Earnings API | `handler/earnings.go` + `service/earnings.go` — GET /earnings | `c7a5615` |
| 11 | Middleware | `middleware/jwt.go`, `requestid.go`, `logger.go`, `recovery.go` | 各 commit |
| A1 | Rate Limiting Middleware | `middleware/ratelimit.go` — per-IP token bucket (auth 5r/s, protected 20r/s) | `6f03141` |
| A2 | User Profile API | `handler/user.go` + `service/user.go` — GET /me, PUT /me/password | `187d305` |
| A3 | Redis Integration | `infra/redis.go` — Upstash go-redis/v9 + health check degraded mode | `c324fb6` |
| A4 | Notification Service | `notification/notifier.go` + `resend.go` — Resend email (welcome, API key alert, generic) | `6add1b8` |
| A5 | Execution Records | `domain/execution.go` + `executions` table + repo/service/handler 全套 — GET /executions | `30a3315` |
| A6 | Billing API | `domain/billing.go` + `billing_records` table + `users.plan` 欄位 + 月費方案 — GET /billing, GET /billing/plan | `915443b` |
| B1 | Bitfinex WebSocket Client | `bitfinex/ws.go` + `ws_types.go` — 公開+認證頻道, 自動重連, heartbeat, DMS | `423c6ec` |
| B2 | Domain: Market Types | `domain/snapshot.go`, `signal.go`, `regime.go` — 市場快照、信號、體制型別 | `9d078ee` |
| B3 | MarketSnapshot Cache | `repository/redis/snapshot.go` + `pubsub.go` — Redis 快取 + Pub/Sub 廣播 | `b4877e4` |
| C1 | Market Feed Service | `lending/marketfeed/service.go` + `snapshot.go` — WS 數據→信號→快照主循環 | `0f58a40` |
| C2 | Flash Crash Detection | `lending/marketfeed/flashcrash.go` — 閃崩偵測 + cooldown 凍結機制 | `0f58a40` |
| C3 | Signal Modules | `lending/signal/` — 6 信號源 (book, liquidation, momentum, margin, crossccy, intraday) + MDC 加權聚合器 | `30276ac` |
| C4 | Order Book Analysis | `lending/orderbook/` — dust filter, wall detection (single+distributed), hidden ratio, competitor | `2052afd` |
| C5 | Regime Detection | `lending/signal/regime.go` — 體制識別 (contango/backwardation/neutral/crisis) + hysteresis | `f429899` |
| D1 | Pricing Strategy | `domain/decision.go` + `lending/strategy/pricing.go` — DecisionContext/Result types + MDC 溢價映射 + regime 調整 | `82ca00b` |
| D2 | Floor Strategy | `lending/strategy/floor.go` — 三層利率地板 (機會成本 + FRR 相對 + regime 動態) | `aad3dbf` |
| D3 | Period Strategy | `lending/strategy/period.go` — regime 驅動天數 + 利率縮放 + 波動率折扣 | `b2faaa7` |
| D4 | Lockup Cost | `lending/strategy/lockup.go` — 鎖倉機會成本折扣 + regime 放大 + 天數建議 | `a7bddfb` |
| D5 | Allocation Strategy | `lending/strategy/allocation.go` — 資金分層部署 (1-3 tiers) + regime 調整 | `8ea7228` |
| D6 | Splitting Strategy | `lending/strategy/splitting.go` — 深度感知掛單拆分 (max 5%, 上限 5 筆) | `8292b0f` |
| D7 | Market Impact | `lending/strategy/impact.go` — 自身市場衝擊管理 (三級門檻 + 利率溢價 + 縮量) | `6c78b8a` |
| D8 | Queue Position | `lending/strategy/queue.go` — 排隊位置估算 + 利率折讓 + stale offer 取消 | `23a4c63` |
| D9 | Partial Fill | `lending/strategy/partial.go` — 殘留偵測 + fillProxy 成交活躍度 + 金額調整 | `d478436` |
| D10 | Stagger | `lending/strategy/stagger.go` — 到期分佈分析 + 最佳 period 選擇 + 集中度偵測 | `21e20c5` |
| D11 | Weekend Premium | `lending/strategy/weekend.go` — 週末遞減溢價 (週五晚+2%, 週六+5%, 週日+3%) | `6311117` |
| D12 | Event Calendar | `lending/strategy/calendar.go` — 月末/季度交割事件溢價 + period 縮短 | `14136bf` |
| D13 | Noise | `lending/strategy/noise.go` — 隨機擾動 (rate ±1%, amount ±2%) + 心理價位避讓 | `2dffc8e` |
| E1 | Offer Execution | `lending/execution/offer.go` — 掛單/撤單執行 + ExecutionRecord 記錄 + 部分失敗容忍 | |
| E2 | Credit Management | `lending/execution/credit.go` — 債權管理 + Auto-Renew (到期偵測 + 續約 + RenewSummary) | |
| E3 | Batch Expiry | `lending/execution/batch.go` — 批次到期優化 (按日分組 + 加權平均 rate + 超限拆分) | |
| E4 | Interest Reinvest | `lending/execution/interest.go` — 利息再投資 (閒置偵測 + 保守參數 + 門檻控制) | |
| E5 | Worker Pool | `lending/worker/pool.go` — Per-User Worker 池 (Start/Stop/StopAll/Reload + auto-cleanup + thread-safe) | |
| E6 | Worker Lifecycle | `lending/worker/worker.go` + `lifecycle.go` — 主循環 (snapshot-driven tick + panic recovery + config 熱載入 + 狀態機) | |
| E7 | API Quota Allocator | `lending/quota/allocator.go` — 雙層配額 (全局 + per-user) + 固定 window refill + thread-safe | |
| E8 | Engine Orchestrator | `lending/service.go` — 引擎頂層編排 (worker.Pool + quota.Allocator + snapshot 廣播 + plan-based quota) | |
| INT | Engine Integration | `lending/factory.go` + `fetcher.go` + `adapter.go` + `main.go` 改寫 — WorkerDepsFactory + service↔lending 橋接 + fx lifecycle + 刪除 engine/ | |
| PH | Production Hardening | Circuit Breaker (gobreaker) + Rate Limiter (token bucket) on Bitfinex client, cursor-based pagination (execution + billing), engine health check, context propagation, deterministic startup (ready channel) | `6894ae4` |
| F1 | Project Scaffold | Next.js 16 + Tailwind v4 + shadcn/ui + Zustand + TanStack Query + React Hook Form + nuqs + Biome + Knip + next-intl (en/zh-TW) | `eea352a` |
| F2 | Core Lib & Types | `api-client.ts`, `format.ts`, `env.ts`, `utils.ts`, `types/index.ts` | `e42bebe` |
| F3 | API Proxy & Cookie Auth | `/api/proxy/[...path]` 同源代理 + login Server Action + middleware 權限檢查 | `6315ad7` |
| F4 | Auth Pages | `(auth)/login` + `(auth)/register` + Zod 表單驗證 | `e8c91c2` |
| F5 | Dashboard Layout | `(dashboard)/layout.tsx` — 側邊欄 + 頂部列 + mobile responsive | `d4517b7` |
| F6 | Overview Page | 統計卡片 + offers list + market panel + chart placeholder | `dbdec32` |
| F7 | API Key Management | API Key CRUD 頁面 + verify + premium dark theme | `519e06d` |
| F8 | Strategy Config | 三層參數表單 + APR conversion + Zod validation | `86f7c6d` |
| F9 | History Page | execution + billing tables + cursor pagination | `74d1f5b` |
| F10 | Settings Page | profile card + change password form | `4275159` |
| F11 | Marketing Pages | landing page + pricing page + marketing layout | `47688cc` |
| F12 | WebSocket Backend | Go `handler/ws.go` — Hub + ws-token + snapshot broadcast | `18c483d` |
| F13 | WebSocket Frontend | `ws-client.ts` + Zustand store + live dashboard | `b19de60` |
| F14 | Charts | Recharts — earnings history API + charts | `f5f8237` |
| F15 | Production Hardening | Sentry + CSP + security headers + env fail-fast | `a014da0` |
| F16 | Testing | Vitest unit tests + Playwright E2E setup | `358ea07` |
| H1 | Axiom Backend Integration | Go zap → Axiom adapter (Tee 模式) + shutdown flush | `70db7a7` |
| H2 | Axiom Frontend Integration | Next.js next-axiom — Web Vitals + server-side logs | `70db7a7` |
| G0 | Composite Strategy Pipeline | 13 策略模組串連成 4 階段 pipeline（Rate→Period→Structure→Adjustments）+ exported helpers | `a86e34b` |
| GT1 | `frrFloorRatio` 0.80→0.92 | 停止低於 FRR 均衡賤賣資金 | `a86e34b` |
| GT2 | `maxPremiumDown` 0.30→0.18 | Backwardation 減少過度降價 | `a86e34b` |
| GT3 | `regimeContangoPeriodRatio` 0.75→0.88 | Contango 更積極鎖定長期高利率 | `a86e34b` |
| GT4 | `rateNoiseRange` 0.01→0.004 | 減少排隊位置損失 | `a86e34b` |
| GT5 | `aggressiveRateMul` 1.25→1.12 | 提高 aggressive tier fill rate | `a86e34b` |
| M1 | `FeeRate` 常數 | 新增 Bitfinex 15% 手續費常數供後續計算使用 | `a86e34b` |
| P2 | Timing & Fill Rate (7 項) | GT6 sigmoid + G12 FRR Trend + G15 Smart Wall + S1 BestAsk Pricing + S5 Cascade + S6 Dynamic Weekend + M5 Rate Spike | `03f63ad` |
| P3 | Data-Driven (5 項) | G14 Auto-Renew Re-pricing + G16 Gap Detection + S4 RatePercentile + S10 Mean-Reversion + M2 Gap Cost Tracking | `12829a2` |

### 目前 DB Schema (5 tables)

- `users` (id, email, password_hash, status, **plan**, created_at, updated_at)
- `api_keys` (id, user_id, label, api_key, api_secret, exchange_status, created_at, updated_at)
- `user_configs` (id, user_id, config JSONB, created_at, updated_at)
- `executions` (id, user_id, action, currency, amount, rate, period, offer_id, status, error_message, created_at)
- `billing_records` (id, user_id, period_start, period_end, plan, amount, currency, status, paid_at, created_at)

### 目前 API Endpoints

```
GET    /api/v1/health
POST   /api/v1/auth/register       (rate limit: 5r/s)
POST   /api/v1/auth/login          (rate limit: 5r/s)
POST   /api/v1/auth/refresh        (rate limit: 5r/s)
POST   /api/v1/auth/verify-email   (rate limit: 5r/s)
POST   /api/v1/auth/forgot-password (rate limit: 5r/s)
POST   /api/v1/auth/reset-password (rate limit: 5r/s)
POST   /api/v1/auth/logout         (JWT)
POST   /api/v1/auth/ws-token       (JWT)
GET    /api/v1/me                  (JWT, 20r/s)
PUT    /api/v1/me/password         (JWT, 20r/s)
POST   /api/v1/api-keys             (JWT)
GET    /api/v1/api-keys             (JWT)
GET    /api/v1/api-keys/:id         (JWT)
DELETE /api/v1/api-keys/:id         (JWT)
POST   /api/v1/api-keys/:id/verify  (JWT)
PUT    /api/v1/configs             (JWT)
GET    /api/v1/configs             (JWT)
DELETE /api/v1/configs             (JWT)
GET    /api/v1/dashboard           (JWT)
GET    /api/v1/earnings            (JWT)
GET    /api/v1/earnings/history    (JWT)
GET    /api/v1/executions          (JWT, cursor pagination: ?after=&limit=)
GET    /api/v1/billing             (JWT, cursor pagination: ?after=&limit=)
GET    /api/v1/billing/plan        (JWT)
GET    /api/v1/ws                  (WebSocket upgrade)
```

### 訂閱方案

| Plan | 月費 | 功能 |
|------|------|------|
| free | $0 | 基本儀表板、手動放貸 |
| starter | $9.99 | 自動放貸（固定利率策略）、email 通知 |
| pro | $29.99 | 進階策略（市場分析、多策略）、優先 API 配額 |
| enterprise | $99.99 | 所有功能、專屬支援、自訂策略參數 |

### 非 DB 持久化的 Domain Types

這些 domain types 無對應 DB table，由 Bitfinex API 即時取得，供 lending engine、dashboard、earnings 使用：

- `domain/wallet.go` — Wallet（fetcher 拉取餘額）
- `domain/offer.go` — FundingOffer, OfferParams（fetcher 拉取 + execution 掛單）
- `domain/credit.go` — FundingCredit（fetcher 拉取 + execution 債權管理）
- `domain/earning.go` — FundingEarning（earnings service 收益統計）

### 放貸引擎市場分析 Pipeline (Phase B+C)

```
bitfinex/ws.go (B1)
    │ ticker, book, trades
    ▼
marketfeed/service.go (C1)
    ├── Phase 1.5: flashcrash.go (C2) → FlashFreeze bool
    ├── Phase 2: signal/*.go (C3) → []SignalValue
    │       book, liquidation, momentum, margin, crossccy
    ├── Phase 2.5: signal/mdc.go (C3) → MDCResult
    ├── Phase 3: signal/regime.go (C5) → RegimeType + RegimeParams
    ├── orderbook/*.go (C4) → dust filter, walls, hidden ratio, competitor
    └── snapshot.go → MarketSnapshot → Redis cache (B3) + Go channel
```

---

## 待開發功能

> 現行軌道的待辦見上方「現行軌道」節（首次 canary deploy + G2 audit）。
> 以下為 Go-era 殘留 backlog。

### ~~Phase H — 運維（Axiom 集中式日誌 + 監控）~~ ❌ 作廢

> **Axiom 已於 2026-05-25（Phase 3c）完全 retire**，改用 PG event store：
> operational telemetry → stdout、forensics → `diagnostics` 表、SoT → `event_log` 表、
> smoke 驗證 → L3 PG-backed HTTP endpoint。原 H3「Axiom Dashboard & Alerts」隨之作廢。

| # | 功能 | 狀態 |
|---|------|------|
| ~~H3~~ | ~~Axiom Dashboard & Alerts~~ | ❌ 作廢（Axiom retired，見上） |

### Phase G — 策略行為增強

> 對照 `strategy_specification.md`（§11-13）+ `docs/strategy-journal.md`。
> 進度：37/41 完成。

#### G0 — 策略 Pipeline 接線 ✅

- [x] G0 Composite Strategy Pipeline — 13 模組串連成 4 階段 pipeline（`12c9e25`）

#### G-Tune — 參數調校 ✅

- [x] GT1 `frrFloorRatio` 0.80 → **0.92**（`12c9e25`）
- [x] GT2 `maxPremiumDown` 0.30 → **0.18**（`12c9e25`）
- [x] GT3 `regimeContangoPeriodRatio` 0.75 → **0.88**（`12c9e25`）
- [x] GT4 `rateNoiseRange` 0.01 → **0.004**（`12c9e25`）
- [x] GT5 `aggressiveRateMul` 1.25 → **1.12**（`12c9e25`）
- [x] GT6 Queue discount — sigmoid smoothstep 曲線（`03f63ad`）

#### G1–G10 — 規範定義增強 ✅

- [x] G1 Adaptive Deviation Guard（`853347b`）
- [x] G2 Signal Recovery Smoothing（`57d9b8c`）
- [x] G3 Smart Hidden Offers（`57d9b8c`）
- [x] G4 Term Structure Analysis（`57d9b8c`）
- [x] G5 Early Return Adjustment（`57d9b8c`）
- [x] G6 Cold Start Protocol（`0e1541d`）
- [x] G7 Maintenance Behaviors（`57d9b8c`）
- [x] G8 Opportunity Cost Framework（`31161b1`）
- [x] G9 Graceful Degradation（`bae8d96`）
- [x] G10 Performance Tracking（`6f19484`）

#### G11–G16 — 實作 Review 新增項目（5/6）

- [x] G11 Idle Capital Urgency（`e5825aa`）
- [x] G12 FRR Trend Tracking — FRR EMA 趨勢判斷（`03f63ad`）
- [x] G13 Historical Fill Rate Learning — 數據驅動定價（candle path-crossing 經驗 fill 模型 + `fill_rate_stats` 表 + backtest engine 整合；spec `docs/superpowers/specs/2026-05-26-g13-fill-rate-learning-design.md`）
  - ⚠️ 啟用待辦：① 刷新本地 `.env` Neon 密碼後 `cd backend && uv run alembic upgrade head`（建表）② 跑 `uv run python -m scripts.learn_fill_rate`（填 stats）③ 把 `FillRateModel` 接進 WFO/matrix runner（目前 engine 已能消費 empirical，但 matrix.py 尚未載入 stats → 實際仍 fallback linear）
- [x] G14 Auto-Renew Re-pricing — 到期走 pipeline 重新定價（`12829a2`）
- [x] G15 Smart Wall Positioning — price just below wall（`03f63ad`）
- [x] G16 Order Book Gap Detection — book 空隙報價（`12829a2`）

<details>
<summary>G12–G16 詳細規格</summary>

| # | 功能 | 說明 | 涉及檔案 | 複雜度 |
|---|------|------|---------|--------|
| G12 | FRR Trend Tracking | FRR 的 EMA(30min) vs EMA(4hr) 趨勢判斷。上升趨勢 → 更敢報高價 | `signal/` 新增或 `marketfeed/` | 中 |
| G13 | Historical Fill Rate Learning | 追蹤每個 rate bucket 的實際 fill rate + time-to-fill | 新增 `lending/tracking/fillrate.go` + DB schema | 高 |
| G14 | Auto-Renew Re-pricing | Credit 到期 renew 時走完整 pricing pipeline 重新定價 | `execution/credit.go` | 低 |
| G15 | Smart Wall Positioning | 靠近 wall 改為 price just below wall 搶先成交 | `strategy/pricing.go` | 中 |
| G16 | Order Book Gap Detection | 掃描 book 找 rate 空隙，在無競爭者的 gap 中報價 | 新增 `orderbook/gap.go` + `strategy/pricing.go` | 中 |

</details>

#### S1–S10 — 策略設計 Review 新增項目（8/10）

> 見 `strategy_specification.md` §12。

- [x] S1 BestAsk-Relative Pricing — 定價改為 bestAsk 偏移（`03f63ad`）
- [x] S2 Auto-Renew Always-On（`e5825aa`）
- [x] S3 Remove Random Noise（`e5825aa`）
- [x] S4 RatePercentile Signal — 歷史百分位信號（`12829a2`）
- [x] S5 Cascade Phase Response — 瀑布分三階段回應（`03f63ad`）
- [x] S6 Dynamic Weekend Premium — 歷史 ratio 動態計算（`03f63ad`）
- [x] S7 Per-Currency Parameters — stablecoin / crypto 分組
- [x] S8 Confidence-Scaled Deployment（`e5825aa`）
- [x] S9 Rolling Period Ladder — 到期梯隊管理
- [x] S10 Mean-Reversion P(higher) — EV_wait 均值回歸公式（`12829a2`）

<details>
<summary>S 系列待開發詳細規格</summary>

| # | 功能 | 說明 | 涉及檔案 | 複雜度 |
|---|------|------|---------|--------|
| S1 | BestAsk-Relative Pricing | 定價改為相對 bestAsk 偏移（`bestAsk - tickOffset(MDC)`），反映 FIFO 匹配 | `strategy/pricing.go` 重寫 | 中 |
| S4 | RatePercentile Signal | 新增第 7 信號源：當前利率在 7d 分佈的百分位 | 新增 `signal/percentile.go` + `marketfeed/` | 中 |
| S5 | Cascade Phase Response | 清算瀑布分三階段：早期 2d 捕暴利、中期 7-14d 鎖高利率、後期停止 | `signal/liquidation.go` + `strategy/pricing.go` | 中 |
| S6 | Dynamic Weekend Premium | 固定 +2-5% 改為滾動 4 週 weekend/weekday ratio | `strategy/weekend.go` | 中 |
| S7 | Per-Currency Parameters | MDC 權重和 regime 閾值分 stablecoin / crypto 兩組 | `signal/mdc.go` + `signal/regime.go` + config | 高 |
| S9 | Rolling Period Ladder | 維持 1/3 短 + 1/3 中 + 1/3 長到期結構 | `strategy/period.go` + `strategy/stagger.go` | 高 |
| S10 | Mean-Reversion P(higher) | `P(higher_rate) = Φ((EMA_7d - current) / σ√t)` | `strategy/floor.go` 或新增 | 中 |

</details>

#### M1–M8 — 市場微觀結構 Review 新增項目（7/8）

> 見 `strategy_specification.md` §13。

- [x] M1 Fee-Adjusted Calculations — `FeeRate = 0.15` 常數（`12c9e25`）
- [x] M2 Gap Cost Tracking + Pre-scheduling — 到期→成交空檔追蹤（`12829a2`）
- [x] M3 Proactive Offer Refresh（`e5825aa`）
- [x] M4 Early Return Risk Premium（`e5825aa`）
- [x] M5 Non-Liquidation Rate Spike Detector — 非清算性 spike 偵測（`03f63ad`）
- [x] M6 FRR Manipulation Guard（`e5825aa`）
- [x] M7 Temporal Laddering — 跨心跳分批部署
- [x] M8 FRR Feedback Loop Awareness（`e5825aa`）

<details>
<summary>M 系列待開發詳細規格</summary>

| # | 功能 | 說明 | 涉及檔案 | 複雜度 |
|---|------|------|---------|--------|
| M2 | Gap Cost Tracking + Pre-scheduling | 追蹤 `avgGapMinutes`，到期前預排程，`gapCost` 作為 period 輸入 | `worker/worker.go` + `execution/credit.go` | 中 |
| M5 | Non-Liquidation Rate Spike Detector | `rate > EMA_1h × 2.0` → 即時短期放貸 | 新增 `signal/ratespike.go` | 中 |
| M7 | Temporal Laddering | 跨心跳分批部署（1/3 per tick），類似 DCA | `worker/worker.go` + `strategy/allocation.go` | 中 |

</details>

---

## 統計

| 類別 | 數量 |
|------|------|
| 已完成 | 103 項 |
| ~~Phase A（CRUD + 基礎設施）~~ | ~~6 項~~ ✅ |
| ~~Phase B（WebSocket + 市場數據）~~ | ~~3 項~~ ✅ |
| ~~Phase C（市場分析層）~~ | ~~5 項~~ ✅ |
| ~~Phase D（策略決策層）~~ | ~~13 項~~ ✅ |
| ~~Phase E（執行層 + Worker）~~ | ~~8 項~~ ✅ |
| ~~Phase F（前端）~~ | ~~16 項~~ ✅ |
| Phase G（策略行為增強） | 41/41 完成 ✅（G13 已實作，待啟用見上） |
| ~~Phase H（運維 — Axiom）~~ | ❌ Axiom 已 retire（Phase 3c），H1–H3 全作廢，改用 PG event store |
| ~~Phase I（CI/CD）~~ | ~~I1+I2~~ ✅ |
| ~~Phase J（Auth 強化）~~ | ~~J1+J2+J3+J5+J6~~ ✅ |
| ~~Phase K（前端測試）~~ | ~~K2~~ ✅ |
| ~~Phase L（UX 強化）~~ | ~~L1+L2+L3+L4+L5~~ ✅ |
| **Go-era 待開發合計** | **0 項** ✅（G13 已實作；現行軌道待辦見上方「現行軌道」節） |

## 依賴關係

```
Phase A ✅ 全部完成
    │
    ├── A3 (Redis) ✅ ──→ Phase B ✅ 全部完成
    │                      │
    │                      ▼
    │                  Phase C ✅ 全部完成
    │                      │
    │                      ▼
    │                  Phase D ✅ 全部完成 (13 策略模組)
    │                      │
    │                      ▼
    ├── A5 (Execution) ✅ → Phase E ✅ 全部完成
    │                      │
    │                      ▼
    │                  Phase G（41/41 完成 ✅）
    │                      ├── G0-G16 ✅ + GT1-6 ✅ + M1-M8 ✅ + S1-S10 ✅
    │                      └── G13 ✅（待啟用見「待開發功能」節）
    │
    └── All backend APIs ready
         │
         ├── Phase F ✅ 全部完成 (16 項)
         │
         └── ~~Phase H (運維 — Axiom)~~ ❌ Axiom retired（Phase 3c）→ 改用 PG event store
```

## 建議開發順序

1. ~~**A1 → A2** — 快速補齊 CRUD，低複雜度~~ ✅
2. ~~**A3** — Redis 整合~~ ✅
3. ~~**A4** — Notification~~ ✅
4. ~~**A5 → A6** — Execution + Billing 完善資料層~~ ✅
5. ~~**B1 → B2 → B3** — WebSocket + 市場數據基礎~~ ✅
6. ~~**C1 → C2 → C3 → C4 → C5** — 市場分析層（放貸引擎核心）~~ ✅
7. ~~**D1-D13** — 策略決策模組~~ ✅
8. ~~**E8** — Engine Orchestrator~~ ✅
9. ~~**F1 → F2 → F3 → F4** — 前端基礎建置~~ ✅
10. ~~**F5 → F6** — Dashboard layout + Overview 頁面~~ ✅
11. ~~**F7 → F8 → F9 → F10** — Dashboard 各功能頁面~~ ✅
12. ~~**F11** — Marketing pages~~ ✅
13. ~~**F12 → F13 → F14** — WebSocket 即時推送 + Charts~~ ✅
14. ~~**F16** — 前端測試~~ ✅
15. ~~**F15** — 前端 Production hardening（Sentry + CSP）~~ ✅
16. ~~**H1, H2** — Axiom 日誌整合~~ ✅
17. ~~**H3** — Axiom Dashboard & Alerts~~ ❌ 作廢（Axiom retired，Phase 3c）

#### ~~P0 — Pipeline 接線~~ ✅

- [x] G0 Composite Strategy Pipeline（`12c9e25`）
- [x] GT1-GT5 參數調校（`12c9e25`）
- [x] M1 FeeRate 常數（`12c9e25`）

#### ~~P1 — 快速收益~~ ✅

- [x] G11 閒置資金急迫度（`e5825aa`）
- [x] S2 Auto-Renew Always-On（`e5825aa`）
- [x] S3 Remove Random Noise（`e5825aa`）
- [x] M3 Proactive Offer Refresh（`e5825aa`）
- [x] M4 Early Return Risk Premium（`e5825aa`）
- [x] M6 FRR Manipulation Guard（`e5825aa`）
- [x] M8 FRR Feedback Loop Awareness（`e5825aa`）
- [x] S8 Confidence-Scaled Deployment（`e5825aa`）

#### ~~P4 — 規範增強~~ ✅（由 remote session 完成）

- [x] G1-G10 全部完成（`853347b` ~ `6f19484`）

#### ~~P2 — 擇時與 fill rate 提升（7/7）~~ ✅

- [x] G12 FRR Trend Tracking（`03f63ad`）
- [x] G15 Smart Wall Positioning（`03f63ad`）
- [x] GT6 Queue discount sigmoid（`03f63ad`）
- [x] S1 BestAsk-Relative Pricing（`03f63ad`）
- [x] S5 Cascade Phase Response（`03f63ad`）
- [x] S6 Dynamic Weekend Premium（`03f63ad`）
- [x] M5 Non-Liquidation Rate Spike Detector（`03f63ad`）

#### ~~P3 — 數據驅動 + 結構優化（5/5）~~ ✅

- [x] G14 Auto-Renew Re-pricing（`12829a2`）
- [x] G16 Order Book Gap Detection（`12829a2`）
- [x] S4 RatePercentile Signal（`12829a2`）
- [x] S10 Mean-Reversion P(higher)（`12829a2`）
- [x] M2 Gap Cost Tracking + Pre-scheduling（`12829a2`）

#### P5 — 高複雜度架構（3/4）

- [x] S7 Per-Currency Parameters
- [x] S9 Rolling Period Ladder
- [x] M7 Temporal Laddering
- [x] G13 Historical Fill Rate Learning
