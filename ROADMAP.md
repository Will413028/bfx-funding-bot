# 開發路線圖

> 最後更新：2026-03-08
> 參考文件：`backend_architecture.md`

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
| 9 | Dashboard API | `handler/dashboard.go` + `service/dashboard.go` — GET /dashboard | `760d11b` |
| 10 | Earnings API | `handler/earnings.go` + `service/earnings.go` — GET /earnings | `c7a5615` |
| 11 | Middleware | `middleware/jwt.go`, `requestid.go`, `logger.go`, `recovery.go` | 各 commit |
| A1 | Rate Limiting Middleware | `middleware/ratelimit.go` — per-IP token bucket (auth 5r/s, protected 20r/s) | `6f03141` |
| A2 | User Profile API | `handler/user.go` + `service/user.go` — GET /me, PUT /me/password | `187d305` |
| A3 | Redis Integration | `infra/redis.go` — Upstash go-redis/v9 + health check degraded mode | `c324fb6` |
| A4 | Notification Service | `notification/notifier.go` + `resend.go` — Resend email (welcome, API key alert, generic) | `6add1b8` |
| A5 | Execution Records | `domain/execution.go` + `executions` table + repo/service/handler 全套 — GET /executions | `30a3315` |
| A6 | Billing API | `domain/billing.go` + `billing_records` table + `users.plan` 欄位 + 月費方案 (free/starter/pro/enterprise) — GET /billing, GET /billing/plan | `915443b` |

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
GET    /api/v1/me                  (JWT, 20r/s)
PUT    /api/v1/me/password         (JWT, 20r/s)
POST   /api/v1/apikeys             (JWT)
GET    /api/v1/apikeys             (JWT)
GET    /api/v1/apikeys/:id         (JWT)
DELETE /api/v1/apikeys/:id         (JWT)
POST   /api/v1/apikeys/:id/verify  (JWT)
PUT    /api/v1/configs             (JWT)
GET    /api/v1/configs             (JWT)
DELETE /api/v1/configs             (JWT)
GET    /api/v1/dashboard           (JWT)
GET    /api/v1/earnings            (JWT)
GET    /api/v1/executions          (JWT)
GET    /api/v1/billing             (JWT)
GET    /api/v1/billing/plan        (JWT)
```

### 訂閱方案

| Plan | 月費 | 功能 |
|------|------|------|
| free | $0 | 基本儀表板、手動放貸 |
| starter | $9.99 | 自動放貸（固定利率策略）、email 通知 |
| pro | $29.99 | 進階策略（市場分析、多策略）、優先 API 配額 |
| enterprise | $99.99 | 所有功能、專屬支援、自訂策略參數 |

### 目前未使用的 Domain Types

這些 domain types 已定義但僅供 Bitfinex client 及 dashboard/earnings service 使用，尚無對應 DB 持久化：

- `domain/wallet.go` — Wallet
- `domain/offer.go` — FundingOffer, OfferParams
- `domain/credit.go` — FundingCredit
- `domain/earning.go` — FundingEarning

---

## 待開發功能

### Phase B — Bitfinex WebSocket & 市場數據基礎

為放貸引擎進階版提供即時市場數據。

| # | 功能 | 說明 | 涵蓋項目 | 複雜度 |
|---|------|------|---------|--------|
| B1 | Bitfinex WebSocket Client | 公開頻道 + 認證頻道 + 自動重連 | `bitfinex/ws.go` + `types.go` | 高 |
| B2 | Domain: Market Types | 市場快照、信號、市場體制型別定義 | `domain/snapshot.go`, `signal.go`, `regime.go` | 中 |
| B3 | MarketSnapshot Cache | Redis 快取 + Pub/Sub 多機廣播 | `repository/redis/snapshot.go` + `pubsub.go` | 中 |

### Phase C — 放貸引擎：市場分析層

共享市場數據服務（Phase 0-3），為所有用戶提供統一市場視角。

| # | 功能 | 說明 | 架構文件對應 | 複雜度 |
|---|------|------|-------------|--------|
| C1 | Market Feed Service | 共享市場數據主循環：數據拉取 → 信號計算 → 廣播 | `lending/marketfeed/service.go` + `snapshot.go` | 高 |
| C2 | Flash Crash Detection | 閃崩偵測 + 凍結機制 | `lending/marketfeed/flashcrash.go` | 中 |
| C3 | Signal Modules | 6 個信號源：MDC、Order Book 消耗、清算瀑布、雙速 VWAP、保證金、跨幣種 | `lending/signal/` (mdc, book, liquidation, momentum, margin, crossccy, intraday) | 極高 |
| C4 | Order Book Analysis | Dust Filter、巨單牆、Hidden Ratio、競爭者偵測 | `lending/orderbook/` (dust, wall, hidden, competitor) | 高 |
| C5 | Regime Detection | 市場體制識別 | `lending/signal/regime.go` | 中 |

### Phase D — 放貸引擎：策略決策層

13 個策略模組，基於市場分析做出利率 / 天數 / 金額決策。

| # | 功能 | 說明 | 架構文件對應 | 複雜度 |
|---|------|------|-------------|--------|
| D1 | Pricing Strategy | 溢價係數 + MDC 映射 | `lending/strategy/pricing.go` | 高 |
| D2 | Floor Strategy | 利率地板 + 機會成本框架 | `lending/strategy/floor.go` | 中 |
| D3 | Period Strategy | 天數決策（曲線形態、線性縮放） | `lending/strategy/period.go` | 高 |
| D4 | Lockup Cost | 鎖倉機會成本折扣 | `lending/strategy/lockup.go` | 中 |
| D5 | Allocation Strategy | 資金分層佈署 | `lending/strategy/allocation.go` | 高 |
| D6 | Splitting Strategy | 深度感知掛單拆分 | `lending/strategy/splitting.go` | 高 |
| D7 | Market Impact | 自身市場衝擊管理 | `lending/strategy/impact.go` | 中 |
| D8 | Queue Position | 排隊位置估算 | `lending/strategy/queue.go` | 中 |
| D9 | Partial Fill | 部分成交管理 | `lending/strategy/partial.go` | 中 |
| D10 | Stagger | 到期時間分散 | `lending/strategy/stagger.go` | 中 |
| D11 | Weekend Premium | 週末遞減溢價 | `lending/strategy/weekend.go` | 低 |
| D12 | Event Calendar | 特殊事件日曆 | `lending/strategy/calendar.go` | 低 |
| D13 | Noise | 隨機擾動 + 心理價位避讓 | `lending/strategy/noise.go` | 低 |

### Phase E — 放貸引擎：執行層 & Worker Pool

掛單執行 + Per-User Worker 生命週期管理。完成後取代目前 `engine/` MVP。

| # | 功能 | 說明 | 架構文件對應 | 複雜度 |
|---|------|------|-------------|--------|
| E1 | Offer Execution | 掛單 / 撤單 / 原子化換單 | `lending/execution/offer.go` | 高 |
| E2 | Credit Management | 債權管理 + Auto-Renew | `lending/execution/credit.go` | 中 |
| E3 | Batch Expiry | 批次到期優化 | `lending/execution/batch.go` | 中 |
| E4 | Interest Reinvest | 利息即時再投資 | `lending/execution/interest.go` | 中 |
| E5 | Worker Pool | Per-User Worker 池管理 | `lending/worker/pool.go` | 高 |
| E6 | Worker Lifecycle | 單一 Worker 主循環 + 啟動/暫停/崩潰/重啟 | `lending/worker/worker.go` + `lifecycle.go` | 高 |
| E7 | API Quota Allocator | 全局 API 配額分配 | `lending/quota/allocator.go` | 中 |
| E8 | Engine Orchestrator | 引擎頂層編排，整合所有子模組 | `lending/service.go`（取代 `engine/`） | 高 |

### Phase F — 前端 & 運維

| # | 功能 | 說明 | 複雜度 |
|---|------|------|--------|
| F1 | WebSocket Dashboard | `handler/dashboard.go` 改為 WebSocket 即時推送（目前是 REST） | 中 |
| F2 | Frontend Dashboard | Next.js 16 完整儀表板 | 極高 |
| F3 | Prometheus Metrics | 監控指標收集 + Grafana 儀表板 | 中 |

---

## 統計

| 類別 | 數量 |
|------|------|
| 已完成 | 17 項 |
| ~~Phase A（CRUD + 基礎設施）~~ | ~~6 項~~ ✅ 全部完成 |
| Phase B（WebSocket + 市場數據） | 3 項 |
| Phase C（市場分析層） | 5 項 |
| Phase D（策略決策層） | 13 項 |
| Phase E（執行層 + Worker） | 8 項 |
| Phase F（前端 + 運維） | 3 項 |
| **待開發合計** | **32 項** |

## 依賴關係

```
Phase A ✅ 全部完成
    │
    ├── A3 (Redis) ✅ ──→ Phase B (B3 需要 Redis)
    │                      │
    │                      ▼
    │                  Phase C (市場分析，依賴 B1 WebSocket + B2 Domain Types)
    │                      │
    │                      ▼
    │                  Phase D (策略決策，依賴 C 的 MarketSnapshot + Signal)
    │                      │
    │                      ▼
    ├── A5 (Execution) ✅ → Phase E (執行層，依賴 A5 Execution Records + D 策略)
    │                      │
    │                      ▼
    └── A6 (Billing) ✅ ─→ Phase F (前端需要所有 API ready)
```

## 建議開發順序

1. ~~**A1 → A2** — 快速補齊 CRUD，低複雜度~~ ✅
2. ~~**A3** — Redis 整合~~ ✅
3. ~~**A4** — Notification~~ ✅
4. ~~**A5 → A6** — Execution + Billing 完善資料層~~ ✅
5. **B1 → B2 → B3** — WebSocket + 市場數據基礎 ← **下一步**
6. **C1 → C2 → C3 → C4 → C5** — 市場分析層（放貸引擎核心）
7. **D1-D13** — 策略決策模組（可分批實作，優先 D1-D6）
8. **E1-E8** — 執行層 + Worker Pool（完成後取代 `engine/` MVP）
9. **F1 → F2 → F3** — 前端 + 監控
