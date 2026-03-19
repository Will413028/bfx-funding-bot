# 開發路線圖

> 最後更新：2026-03-19
> 參考文件：`backend_architecture.md`, `frontend_architecture.md`, `strategy_specification.md`

---

## 已完成功能

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
| I1 | GitHub Actions CI | `.github/workflows/ci.yml` — backend (go vet + golangci-lint + go test -race + go build) + frontend (tsc + biome + vitest)，push/PR 觸發，雙 job 平行 | `3bc0151` |
| I2 | Backend Linter | `backend/.golangci.yml` — gocritic, gosec, misspell, copyloopvar 等 + lint fix (rangeValCopy, errorlint, unparam) + dashboard_test mock credit 格式修正 | `3bc0151` |
| J3 | CORS Middleware | `router.go` — `gin-contrib/cors` 已於初始實作中配置 `FRONTEND_URL` 白名單（review 時誤判為缺口） | 既有 |
| J5 | Per-User Rate Limit | `middleware/userratelimit.go` — per-user token bucket (10r/s, burst 20) + auto-cleanup + 5 tests，掛載於 protected routes | `d4c5966` |
| G1 | Adaptive Deviation Guard | `strategy/pricing.go` — per-regime FRR deviation ceiling (牛 40%/熊 20%/震盪 25%/危機 60%) + hard ceiling 80% + 6 tests | `853347b` |
| G6 | Cold Start Protocol | `worker/worker.go` — 3 階段 MDC 漸進啟用 (0%→50%→75%→100%, 每階段 10min) + injectable clock + 6 tests | `0e1541d` |
| G9 | Graceful Degradation | `signal/health.go` + `signal/mdc.go` + `marketfeed/service.go` — 信號健康度追蹤 (healthy/warning/degraded/recovering) + MDC 降級權重重分配 + Order Book 故障 FRR-only + 恢復確認 2 心跳 + 13 new tests | `bae8d96` |
| J1 | Email 驗證 | 註冊建立 pending 用戶 + Redis token + Resend 驗證信 + `POST /auth/verify-email` + Login 拒絕未驗證 | |
| J2 | Forgot Password | `POST /auth/forgot-password` + `POST /auth/reset-password` + Redis token (1hr TTL) + anti-enumeration | `745facd` |
| J6 | JWT Refresh Token | access token 15min + refresh token 7d Redis rotation + `POST /auth/refresh` + `POST /auth/logout` + 前端 proxy 透明刷新 + 雙 cookie (auth_token + refresh_token) | |

### 目前 DB Schema (5 tables)

- `users` (id, email, password_hash, status, **plan**, created_at, updated_at)
- `api_keys` (id, user_id, label, api_key, api_secret, exchange_status, created_at, updated_at)
- `user_configs` (id, user_id, config JSONB, created_at, updated_at)
- `executions` (id, user_id, action, currency, amount, rate, period, offer_id, status, error_message, created_at)
- `billing_records` (id, user_id, period_start, period_end, plan, amount, currency, status, paid_at, created_at)

### 目前 API Endpoints

```
GET    /api/v1/health
POST   /api/v1/auth/register        (rate limit: 5r/s)
POST   /api/v1/auth/login           (rate limit: 5r/s)
POST   /api/v1/auth/refresh         (rate limit: 5r/s)
POST   /api/v1/auth/verify-email    (rate limit: 5r/s)
POST   /api/v1/auth/forgot-password (rate limit: 5r/s)
POST   /api/v1/auth/reset-password  (rate limit: 5r/s)
POST   /api/v1/auth/logout          (JWT, protected)
GET    /api/v1/auth/ws-token       (JWT)
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

### Phase H — 運維（Axiom 集中式日誌 + 監控）

| # | 功能 | 說明 | 複雜度 |
|---|------|------|--------|
| H3 | Axiom Dashboard & Alerts | Axiom UI 建立監控面板 + 告警規則（Worker 崩潰率、API 錯誤率、WS 斷線） | 中 |

### Phase G — 策略行為增強

> 對照 `strategy_specification.md`，以下功能在規範中定義但尚未實作。
> 多數屬於現有模組的行為增強，非獨立策略模組。

| # | 規範章節 | 功能 | 說明 | 涉及檔案 | 複雜度 |
|---|----------|------|------|---------|--------|
| ~~G1~~ | ~~§8.1~~ | ~~Adaptive Deviation Guard~~ | ~~掛單利率偏離 FRR 上限保護（per-regime: 牛市 40%、熊市 20%、震盪 25%、危機 60%）~~ | ~~`strategy/pricing.go`~~ | ~~低~~ ✅ |
| G2 | §2.4 | Signal Recovery Smoothing | 信號恢復時 3 步線性遞增權重（33%→66%→100%），防止 MDC 從衰減突然跳回滿額 | `signal/mdc.go` | 中 |
| G3 | §4.3 | Smart Hidden Offers | 根據競爭度 + HiddenRatio 決定使用隱藏單（`flags: 64`）或公開單 | 新增 `strategy/hidden.go` | 中 |
| G4 | §5.1 | Term Structure Analysis | 利率曲線形態偵測（陡峭/駝峰/倒掛/平坦），作為天數決策前置過濾器 | 新增 `strategy/termstructure.go` | 中 |
| G5 | §5.6 | Early Return Adjustment | `有效回報 = Rate × 歷史持有率`，持有率 < 60% 時降低天數偏好 | `strategy/period.go` | 中 |
| ~~G6~~ | ~~§6.4~~ | ~~Cold Start Protocol~~ | ~~Worker 啟動前 30 分鐘分 3 階段逐步啟用信號（目前直接全量運行）~~ | ~~`worker/worker.go`~~ | ~~中~~ ✅ |
| G7 | §7.1 | Maintenance Behaviors | 隊首保留檢查、僵屍單動態 TTL 撤銷、原子化換單（先掛新再撤舊） | `worker/worker.go` + `execution/offer.go` | 中 |
| G8 | §4.8 | Opportunity Cost Framework | 完整 EV_deploy vs EV_wait 比較模型（目前 floor.go 僅有靜態地板） | `strategy/floor.go` 或新增 | 高 |
| ~~G9~~ | ~~§6.3~~ | ~~Graceful Degradation~~ | ~~信號源健康度追蹤（健康/警告/故障）+ 故障時重分配權重 + 恢復確認~~ | ~~`signal/mdc.go` + `marketfeed/service.go`~~ | ~~高~~ ✅ |
| G10 | §9.1-9.3 | Performance Tracking | Alpha 量化、策略模組歸因、自適應參數回饋（每週 ±10% 微調） | 新增 `lending/tracking/` | 極高 |

### Phase I — DevOps / CI/CD

| # | 功能 | 說明 | 複雜度 |
|---|------|------|--------|
| ~~I1~~ | ~~GitHub Actions CI~~ | ~~`go test ./...` + `go vet` + `biome check` + `vitest` — push/PR 觸發~~ | ~~低~~ ✅ |
| ~~I2~~ | ~~Backend Linter~~ | ~~golangci-lint 設定（`.golangci.yml`）— 與 CI 整合~~ | ~~低~~ ✅ |
| I3 | Staging 環境 | Koyeb + Vercel preview 環境，與 production 分離，PR 自動部署 preview | 中 |
| I4 | Koyeb Health Check 設定 | 設定 `/api/v1/health` 為 Koyeb readiness/liveness probe | 低 |

### Phase J — 安全性 & 帳號

| # | 功能 | 說明 | 複雜度 |
|---|------|------|--------|
| ~~J1~~ | ~~Email 驗證~~ | ~~註冊後發送驗證信 + `users.status` 狀態切換（pending → active）~~ | ~~中~~ ✅ |
| ~~J2~~ | ~~Forgot Password~~ | ~~忘記密碼 flow（Resend 發送 reset link + token 驗證 + 重設密碼 endpoint）~~ | ~~中~~ ✅ |
| ~~J3~~ | ~~CORS Middleware~~ | ~~後端明確設定 CORS 白名單（`FRONTEND_URL`）~~ — 已在 `router.go` 中用 `gin-contrib/cors` 實作 | ~~低~~ ✅ |
| ~~J4~~ | ~~CSRF Protection~~ | ~~不適用~~ — 已使用 httpOnly cookie + SameSite=Strict/Lax，token 不暴露給 JS，天然免疫 CSRF | ~~中~~ N/A |
| ~~J5~~ | ~~Per-User Rate Limit~~ | ~~認證後 API 加入 per-user rate limit（目前僅 per-IP），防止單一帳號濫用~~ | ~~低~~ ✅ |
| ~~J6~~ | ~~JWT Refresh Token~~ | ~~實作 refresh token rotation~~ | ~~中~~ ✅ |

### Phase K — 測試補強

| # | 功能 | 說明 | 複雜度 |
|---|------|------|--------|
| K1 | Frontend E2E Tests | Playwright 測試 critical path：註冊 → 登入 → 設定 API Key → Dashboard → 策略設定 | 中 |
| K2 | Frontend Component Tests | features/ 下 hooks + components 的 Vitest 單元測試（目前只覆蓋 lib/） | 中 |
| K3 | Backend Integration Tests | 使用 testcontainers-go 跑真實 PostgreSQL 的 repository 層測試 | 高 |

### Phase L — 前端體驗

| # | 功能 | 說明 | 複雜度 |
|---|------|------|--------|
| L1 | Loading Skeleton | 各頁面加入 Skeleton / Shimmer loading 狀態（目前僅有 global error boundary） | 低 |
| L2 | User Onboarding Flow | 新用戶引導：歡迎 → 設定 API Key → 設定策略 → 啟動引擎，分步引導 | 中 |
| L3 | Per-Page Error Boundary | 各 feature 區塊加入局部 error boundary + retry，避免單一區塊錯誤炸掉整頁 | 低 |
| L4 | PWA Support | `manifest.json` + service worker — 行動裝置加到主畫面 | 低 |
| L5 | Accessibility (a11y) | ARIA labels + 鍵盤導航 + 色彩對比度檢查 | 中 |

### Phase M — 商業邏輯

| # | 功能 | 說明 | 複雜度 |
|---|------|------|--------|
| M1 | Stripe 付款整合 | Stripe Checkout / Customer Portal 串接 — `billing_records` 表已備、需加入 Stripe webhook handler | 高 |
| M2 | 訂閱生命週期管理 | 升降級 plan、到期處理、grace period、取消訂閱 → 自動降級 free plan | 高 |

---

## 統計

| 類別 | 數量 |
|------|------|
| 已完成 | 77 項 |
| ~~Phase A（CRUD + 基礎設施）~~ | ~~6 項~~ ✅ 全部完成 |
| ~~Phase B（WebSocket + 市場數據）~~ | ~~3 項~~ ✅ 全部完成 |
| ~~Phase C（市場分析層）~~ | ~~5 項~~ ✅ 全部完成 |
| ~~Phase D（策略決策層）~~ | ~~13 項~~ ✅ 全部完成 |
| ~~Phase E（執行層 + Worker）~~ | ~~8 項~~ ✅ 全部完成 |
| ~~Phase F（前端）~~ | ~~16 項~~ ✅ 全部完成 |
| Phase G（策略行為增強） | 10 項 |
| ~~Phase H（運維 — Axiom）~~ | ~~H1+H2~~ ✅ 完成，H3 待部署後設定 |
| Phase I（DevOps / CI/CD） | ~~I1+I2~~ ✅ 完成，2 項待開發 |
| Phase J（安全性 & 帳號） | 6 項 |
| Phase K（測試補強） | 3 項 |
| Phase L（前端體驗） | 5 項 |
| Phase M（商業邏輯） | 2 項 |
| **待開發合計** | **20 項** |

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
    │                  Phase G (策略增強，依賴 C+D+E 既有模組)
    │
    ├── All backend APIs ready
    │    │
    │    ├── Phase F ✅ 全部完成 (16 項)
    │    │    │
    │    │    ├── Phase K (測試補強，依賴 F 完成的頁面)
    │    │    └── Phase L (前端體驗，在 F 基礎上增強)
    │    │
    │    └── Phase H (運維 — Axiom)
    │        ✅ H1+H2 完成 → H3 (Dashboard & Alerts，待部署後設定)
    │
    ├── Phase I (DevOps / CI/CD，獨立於功能開發) — I1+I2 ✅
    │
    ├── Phase J (安全性 & 帳號，依賴 A4 Notification + Auth 基礎)
    │    ├── J1 (Email 驗證) → J2 (Forgot Password)
    │    └── J3-J6 獨立可平行
    │
    └── A6 (Billing) ✅ → Phase M (Stripe 整合，依賴 billing schema)
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
17. ~~**I1 → I2** — CI/CD + Linter（低成本高回報，越早建越好）~~ ✅
18. ~~**J3 → J5** — CORS + per-user rate limit（低複雜度安全性修補）~~ ✅
19. ~~**G1 → G6 → G9** — 引擎安全三件套（偏離防護 + 冷啟動 + 降級）~~ ✅
20. ~~**J1 → J2** — Email 驗證 + Forgot Password（帳號安全基礎）~~ ✅
21. ~~**J4 → J6** — CSRF (N/A) + JWT Refresh Token Rotation~~ ✅
22. **L1 → L3** — Loading skeleton + Error boundary（前端體驗基礎）
23. **H3** — Axiom Dashboard & Alerts（部署後在 Axiom UI 設定）
24. **I4** — Koyeb Health Check 設定
25. **K1** — Playwright E2E critical path 測試
26. **G2 → G3 → G4 → G5 → G7** — 策略增強（中複雜度）
27. **L2** — User Onboarding Flow
28. **M1 → M2** — Stripe 整合 + 訂閱管理（需要收費時再做）
29. **G8 → G9** — 高複雜度策略增強
30. **I3** — Staging 環境（用戶量增長後）
31. **K2 → K3** — 補強前端 component + 後端 integration 測試
32. **L4 → L5** — PWA + Accessibility
33. **G10** — 績效追蹤（極高複雜度，需 DB schema 擴充）
