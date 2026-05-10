# Bitfinex 自動放貸 SaaS 平台：後端架構設計文件

> ⚠️ **狀態說明（2026-05-10）**
>
> 後端已從 Go 重寫為 **Python 3.13**（`backend_py/`）。本文件原為 Go 版本撰寫，**多數內容仍是現行設計的 source of truth**：DB schema / API endpoints / 多租戶隔離規則 / 安全模型 / 業務分層思維（Transport → Application → Domain → Infrastructure）一律沿用。
>
> 不再適用的部分：
> - **語法範例**（`gin.H{}` / `sqlc` 生成的 Go struct / `fx.Provide(...)`）只是歷史參考；實作細節以 `backend_py/` 現行 code 為準。
> - **分層的目錄組織**：`backend_py/` 改用 module-per-feature（`modules/<feature>/{tables,schemas,repository,service}.py`），不同層的程式碼**水平共置在 feature 模組內**，而非全域分一個 `handler/`、一個 `service/`、一個 `repository/`。本文件第 2 節描述的「責任區分」概念仍適用（每個 module 內依然有 transport/application/domain/infra 對應的檔案），只是不再有跨 feature 的全域目錄。
> - **Tooling**：見下方 Tech Stack 改寫；Atlas/sqlc/go test 已換成 Alembic/SQLAlchemy ORM/pytest。
>
> Phase 0 重寫的決策過程見 `docs/superpowers/specs/2026-05-10-python-rewrite-day3-stack-decisions.md`。

---

## 1. 技術棧總覽

| 層級 | 技術 | 說明 |
| :--- | :--- | :--- |
| 語言 | Python 3.13 | uv 管理（pyproject.toml + uv.lock）；strict mypy + ruff |
| Web 框架 | FastAPI | async ASGI；OpenAPI 自動產生；Pydantic v2 模型 |
| ORM / DB driver | SQLAlchemy 2.0 async + asyncpg | declarative `Base` + module-per-feature `tables.py` |
| 資料庫 | PostgreSQL (Neon) | 多租戶持久化，WHERE user_id 隔離 |
| 快取 | Redis (Upstash) | Session、MarketSnapshot 快取、Pub/Sub |
| Bitfinex API | 自製 `external/bitfinex/` | httpx async REST client + aiolimiter rate limit；WebSocket（Phase 後段）|
| 加密 | AES-256-GCM | API Key 加密儲存 |
| 認證 | JWT (RS256) | Stateless Token |
| 部署 | Koyeb (Docker) | Git 驅動自動部署 |
| 資料庫託管 | Neon | Serverless PostgreSQL，自動擴縮 |
| 快取託管 | Upstash | Serverless Redis，按用量計費 |
| 監控 | Axiom | 集中式日誌收集 + 儀表板 + 告警（Phase H） |
| DB Migration | Alembic（async + autogenerate） | `backend_py/alembic/`；`alembic upgrade head` / `alembic check` 驗 drift |
| Schema 定義 | SQLAlchemy declarative | 每個 feature 一份 `modules/<feature>/tables.py`，import 進 `alembic/env.py` 觸發 metadata 註冊 |
| DI / lifecycle | FastAPI `Depends` + asynccontextmanager | 取代 Go 時代的 fx；session_scope 用 `core/db.py` 提供的 contextmanager |
| 設定 | pydantic-settings | `core/settings.py` 從 `.env` 讀；`extra="ignore"` 容忍共用 .env 的 Go-era 殘留變數 |
| 測試 | pytest + pytest-asyncio + pytest-httpx + aiosqlite | Unit 測用 in-memory sqlite；integration 標 `@pytest.mark.integration` 預設 skip |

---

## 2. 分層架構

```
┌──────────────────────────────────────────────────────┐
│                    Transport 層                       │
│  handler/    → HTTP 請求處理、參數驗證、回應格式     │
│  middleware/ → JWT、Rate Limit                        │
├──────────────────────────────────────────────────────┤
│                   Application 層                      │
│  service/  → CRUD 業務編排（用戶、API Key、計費）    │
│  lending/  → 放貸引擎（獨立生命週期，非 HTTP 驅動） │
├──────────────────────────────────────────────────────┤
│                     Domain 層                         │
│  domain/  → 純型別定義 + 業務規則（零外部依賴）      │
├──────────────────────────────────────────────────────┤
│                 Infrastructure 層                     │
│  repository/ → PostgreSQL + Redis 資料存取             │
│  bitfinex/   → 交易所 API 封裝                       │
│  notification/ → Email 通知                          │
│  crypto/     → 加解密                                │
└──────────────────────────────────────────────────────┘
```

**核心設計原則**：依賴方向嚴格由上往下。Domain 層不依賴任何其他層。上層透過 interface 依賴下層，不直接依賴具體實作。

### JSON 慣例

#### 命名：camelCase

所有 API 回應的 JSON key 一律使用 **camelCase**：

```go
// ✅ 正確
type WalletSummary struct {
    BalanceAvailable float64 `json:"balanceAvailable"`
    CreatedAt        time.Time `json:"createdAt"`
}

// ❌ 錯誤（snake_case）
type WalletSummary struct {
    BalanceAvailable float64 `json:"balance_available"`
    CreatedAt        time.Time `json:"created_at"`
}
```

**適用範圍**：
- struct 的 `json:"..."` tag
- `gin.H{}` 的 key
- 前端接收的 JSON 請求/回應 body

#### 回應格式：統一 `data` 包裝

所有成功回應一律用 `{ "data": ... }` 包裝，錯誤回應用 `{ "error": ... }` 包裝。前端可透過有無 `data` key 區分成功/失敗。

```go
// ✅ 成功回應（單一物件）
c.JSON(http.StatusOK, gin.H{"data": gin.H{"id": "...", "email": "..."}})

// ✅ 成功回應（分頁列表）— pagination 與 data 同層
c.JSON(http.StatusOK, gin.H{
    "data":       records,
    "pagination": PaginationResponse{...},
})

// ✅ 錯誤回應
c.JSON(statusCode, gin.H{"error": gin.H{"code": "...", "message": "..."}})

// ✅ 204 No Content — 無 body
c.Status(http.StatusNoContent)
```

**無例外**：所有 endpoint（含 `GET /health`）皆使用 `data` 包裝。

---

## 3. 目錄結構

```
backend/
├── cmd/
│   └── server/
│       └── main.go                      # 應用入口點
│
├── internal/
│   │
│   │   # ── Transport 層 ──
│   ├── handler/
│   │   ├── router.go                    # 路由註冊
│   │   ├── auth.go                      # 註冊 / 登入 / JWT 簽發
│   │   ├── user.go                      # 用戶 CRUD (GET /me, PUT /me/password)
│   │   ├── apikey.go                    # API Key 管理
│   │   ├── config.go                    # 策略參數 CRUD
│   │   ├── dashboard.go                 # Dashboard 摘要 (REST，未來改 WebSocket)
│   │   ├── earnings.go                  # 收益統計
│   │   ├── execution.go                 # 放貸執行紀錄查詢（cursor-based pagination）
│   │   ├── billing.go                   # 帳單查詢（cursor-based pagination）+ 訂閱方案
│   │   ├── health.go                    # 健康檢查 (Postgres + Redis + Engine Status)
│   │   └── pagination.go               # 游標分頁 (cursor-based pagination)
│   │
│   ├── middleware/
│   │   ├── jwt.go                       # JWT 驗證
│   │   ├── ratelimit.go                 # Per-IP token bucket 頻率限制
│   │   ├── requestid.go                 # X-Request-ID 注入
│   │   ├── logger.go                    # 請求日誌 (zap)
│   │   └── recovery.go                  # Panic 恢復
│   │
│   │   # ── Application 層：CRUD 業務 ──
│   ├── service/
│   │   ├── user.go                      # 用戶註冊 / 登入 / Profile / 改密碼
│   │   ├── apikey.go                    # API Key CRUD + 加密 + 權限驗證
│   │   ├── config.go                    # 策略參數管理 + 通知 Worker 熱載入
│   │   ├── dashboard.go                 # Dashboard 摘要（wallet + offers + credits + market snapshot）
│   │   ├── earnings.go                  # 收益統計（daily estimate, APY, 7d/30d）
│   │   ├── execution.go                 # 放貸執行紀錄查詢
│   │   └── billing.go                   # 帳單查詢 + 訂閱方案功能權限
│   │
│   │   # ── Application 層：放貸引擎 ──
│   ├── lending/
│   │   ├── service.go                   # 引擎入口：Worker Pool + Quota + Snapshot 廣播
│   │   ├── factory.go                   # WorkerDepsFactory 實作（組裝 Worker 依賴）
│   │   ├── fetcher.go                   # DataFetcher 實作（Bitfinex REST 拉取用戶資料）
│   │   ├── adapter.go                   # OfferExecutor adapter（execution → worker 型別轉換）
│   │   │
│   │   ├── marketfeed/                  # 共享市場數據服務（Phase 0-3）
│   │   │   ├── service.go               #   主循環：數據拉取 → 信號計算 → 廣播
│   │   │   ├── snapshot.go              #   MarketSnapshot 組裝
│   │   │   ├── flashcrash.go            #   閃崩偵測（Phase 1.5）
│   │   │   └── interfaces.go            #   SignalSource interface 定義
│   │   │
│   │   ├── signal/                      # 信號計算模組（13 files）
│   │   │   ├── mdc.go                   #   MDC 計算 + per-currency preset 權重（S7）
│   │   │   ├── book.go                  #   Order Book 消耗速度
│   │   │   ├── liquidation.go           #   清算瀑布偵測
│   │   │   ├── momentum.go              #   雙速 VWAP
│   │   │   ├── margin.go                #   保證金持倉量
│   │   │   ├── crossccy.go              #   跨幣種 Funding Rate
│   │   │   ├── intraday.go              #   日內時段 + 月內修正
│   │   │   ├── regime.go                #   市場體制識別 + per-currency 門檻（S7）
│   │   │   ├── health.go                #   信號健康度追蹤（G9 三態偵測）
│   │   │   ├── frrtrend.go              #   FRR 趨勢信號（G12）
│   │   │   ├── percentile.go            #   歷史百分位信號（S4）
│   │   │   └── ratespike.go             #   利率飆升偵測（M5）
│   │   │
│   │   ├── orderbook/                   # 掛單簿分析（6 files）
│   │   │   ├── dust.go                  #   動態 Dust Filter
│   │   │   ├── wall.go                  #   巨單牆 + 分散牆偵測
│   │   │   ├── hidden.go                #   Hidden Ratio 估算
│   │   │   ├── competitor.go            #   競爭者行為偵測
│   │   │   ├── gap.go                   #   掛單簿缺口偵測（G16）
│   │   │   └── summary.go              #   掛單簿摘要統計
│   │   │
│   │   ├── strategy/                    # 策略決策（Phase 4，20 files）
│   │   │   ├── composite.go             #   四階段 pipeline 編排（S7 per-currency preset）
│   │   │   ├── pricing.go               #   溢價係數 + MDC 映射（S7 per-currency）
│   │   │   ├── floor.go                 #   利率地板 + 機會成本框架
│   │   │   ├── period.go                #   天數決策 + Rolling Period Ladder（S9）
│   │   │   ├── lockup.go                #   鎖倉機會成本折扣
│   │   │   ├── allocation.go            #   資金分層佈署
│   │   │   ├── splitting.go             #   深度感知掛單拆分
│   │   │   ├── impact.go                #   自身市場衝擊管理
│   │   │   ├── queue.go                 #   排隊位置估算
│   │   │   ├── partial.go               #   部分成交管理
│   │   │   ├── stagger.go               #   到期時間分散
│   │   │   ├── weekend.go               #   週末遞減溢價
│   │   │   ├── calendar.go              #   特殊事件日曆
│   │   │   ├── noise.go                 #   隨機擾動 + 心理價位避讓
│   │   │   ├── earlyreturn.go           #   提前還款風險溢價（G5）
│   │   │   ├── hidden.go                #   Hidden order 策略（G14）
│   │   │   ├── maintenance.go           #   維護模式處理
│   │   │   ├── meanreversion.go         #   均值回歸 P(higher) 計算（S10）
│   │   │   ├── opportunitycost.go       #   機會成本框架
│   │   │   └── termstructure.go         #   期限結構分析
│   │   │
│   │   ├── execution/                   # 掛單執行（Phase 5-6）
│   │   │   ├── offer.go                 #   掛單 / 撤單 / 原子化換單
│   │   │   ├── credit.go                #   債權管理 + Auto-Renew
│   │   │   ├── batch.go                 #   批次到期優化
│   │   │   └── interest.go              #   利息即時再投資
│   │   │
│   │   ├── worker/                      # Per-User Worker（Phase 4-7 編排）
│   │   │   ├── pool.go                  #   Worker Pool 管理
│   │   │   ├── worker.go                #   單一 Worker 主循環
│   │   │   └── lifecycle.go             #   啟動 / 暫停 / 崩潰 / 重啟
│   │   │
│   │   ├── quota/                       # 全局 API 配額分配器
│   │   │   ├── allocator.go             #   公平分配演算法
│   │   │   └── limiter_pool.go          #   Per-user rate limiter pool
│   │   │
│   │   └── tracking/                    # 效能追蹤（G10）
│   │       ├── alpha.go                 #   Alpha 收益追蹤（vs FRR baseline）
│   │       ├── attribution.go           #   策略貢獻歸因
│   │       ├── feedback.go              #   自適應反饋迴路
│   │       └── summary.go              #   效能摘要統計
│   │
│   │   # ── Domain 層 ──
│   ├── domain/
│   │   ├── user.go                      # User, UserStatus (含 Plan 欄位)
│   │   ├── apikey.go                    # APIKey, KeyPermissions
│   │   ├── offer.go                     # FundingOffer, OfferParams
│   │   ├── credit.go                    # FundingCredit, CreditStatus
│   │   ├── wallet.go                    # Wallet
│   │   ├── earning.go                   # FundingEarning
│   │   ├── execution.go                 # ExecutionRecord (place/cancel/filled/renew)
│   │   ├── billing.go                   # BillingRecord, PlanFeatures, 訂閱方案常數
│   │   ├── errors.go                    # AppError 統一錯誤型別
│   │   ├── config.go                    # StrategyConfig（附錄 B 全參數型別定義）
│   │   ├── snapshot.go                  # MarketSnapshot, RawMarketData, OrderBookAnalysis
│   │   ├── signal.go                    # SignalValue, MDCResult
│   │   ├── regime.go                    # RegimeType, RegimeParams
│   │   └── engine.go                    # EngineStatus（引擎運行狀態）
│   │
│   │   # ── Infrastructure 層 ──
│   │
│   ├── infra/                  # 外部服務連線初始化
│   │   ├── di.go                        #   fx Module 聚合
│   │   ├── postgres.go                  #   *pgxpool.Pool 建立 + 健康檢查
│   │   └── redis.go                     #   Redis client 建立 + 健康檢查
│   │
│   ├── repository/
│   │   ├── di.go                        # fx Module 聚合（類似 SosReaderServer）
│   │   ├── interfaces.go                # 所有 Repository interface 集中定義
│   │   │
│   │   ├── postgres/                    # ── PostgreSQL DAO ──
│   │   │   ├── di.go                    #   fx Module：註冊所有 postgres repo
│   │   │   ├── query/                   #   手寫 SQL 查詢（sqlc 輸入）
│   │   │   │   ├── user.sql
│   │   │   │   ├── apikey.sql
│   │   │   │   ├── execution.sql
│   │   │   │   ├── config.sql
│   │   │   │   └── billing.sql
│   │   │   ├── sqlc/                    #   sqlc 自動產生（勿手動修改）
│   │   │   │   ├── db.go                #     DBTX interface
│   │   │   │   ├── models.go            #     DB row struct
│   │   │   │   ├── querier.go           #     Querier interface
│   │   │   │   ├── user.sql.go
│   │   │   │   ├── apikey.sql.go
│   │   │   │   ├── execution.sql.go
│   │   │   │   ├── config.sql.go
│   │   │   │   └── billing.sql.go
│   │   │   ├── user.go                  #   手寫：包裝 sqlc → 實作 Repository interface
│   │   │   ├── apikey.go                #     sqlc model ↔ domain 型別轉換
│   │   │   ├── execution.go
│   │   │   ├── config.go
│   │   │   └── billing.go
│   │   │
│   │   └── redis/                       # ── Redis DAO ──
│   │       ├── di.go                    #   fx Module：註冊所有 redis repo
│   │       ├── session.go               #   Session 儲存（JWT refresh token）
│   │       ├── snapshot.go              #   MarketSnapshot 快取（Dashboard 讀取用）
│   │       └── pubsub.go               #   Pub/Sub（多機 MarketSnapshot 廣播）
│   │
│   ├── bitfinex/
│   │   ├── client.go                    # REST API 封裝（HMAC-SHA384 認證）
│   │   ├── auth.go                      # 認證簽名邏輯
│   │   ├── ws.go                        # WebSocket 連接管理（公開 + 認證）
│   │   └── ws_types.go                  # WebSocket 訊息型別定義
│   │
│   ├── notification/
│   │   ├── notifier.go                  # Notifier interface 定義
│   │   └── resend.go                    # Resend Email 實作 (welcome, alert)
│   │
│   ├── crypto/
│   │   └── aes.go                       # AES-256-GCM 加解密
│   │
│   └── appconfig/
│       └── config.go                    # 環境變數載入（含 Redis, Resend 配置）
│
├── schema/                                    # Atlas 宣告式 Schema（Desired State）
│   ├── schema.hcl                             #   完整資料庫 schema 定義
│   └── atlas.hcl                              #   Atlas 專案配置（env、data source）
│
├── migrations/                                # Atlas 版本化 Migration（自動產生）
│   ├── 20260307182925_create_users.sql
│   ├── 20260307192840_create_api_keys.sql
│   ├── 20260308030457_create_user_configs.sql
│   ├── 20260308035500_add_exchange_status.sql
│   ├── 20260308065022_create_executions.sql
│   ├── 20260308065825_add_plan_and_billing.sql
│   └── atlas.sum                              #   Migration 完整性校驗
│
├── sqlc.yaml                                  # sqlc 配置
├── Makefile
├── go.mod
└── go.sum
```

---

## 4. 各層職責與規範

### 4.1 Transport 層 (`handler/`, `middleware/`)

**職責**：HTTP 請求的進出口。只做參數解析、驗證、回應格式化。不包含業務邏輯。

**規範**：
- handler 只呼叫 `service/`，不直接操作 `repository/` 或 import `lending/`。
- 引擎狀態透過 `EngineHealthProvider` interface + `domain.EngineStatus` 讀取，handler 不感知 `lending/` 的存在。
- 每個 handler 方法不超過 30 行，邏輯複雜時委派給 service。
- 錯誤回應使用統一格式：`{ "error": { "code": "...", "message": "..." } }`。

**呼叫方向**：
```
handler/auth.go      → service/user.go
handler/apikey.go    → service/apikey.go
handler/config.go    → service/config.go → 通知 lending/worker 熱載入
handler/dashboard.go → service/dashboard.go（Bitfinex REST 拉取用戶資料 + Redis 讀取 MarketSnapshot）
handler/health.go    → infra (pgxpool, redis) + EngineHealthProvider (domain.EngineStatus)
```

### 4.2 Application 層 — CRUD 業務 (`service/`)

**職責**：編排 CRUD 業務流程。呼叫 `domain/` 做驗證、`repository/` 做持久化、`notification/` 做通知。

**規範**：
- 每個 service struct 透過建構函式注入依賴（repository interface + 其他 service），由 fx 自動 wire。
- 不直接操作 Bitfinex API——API Key 的權限驗證委派給 `bitfinex/client.go`。
- 與 `lending/` 的溝通透過 **consumer-side interface** 解耦：`service/` 定義小 interface，`lending.Service` 隱式實作，fx 自動注入。

```go
// service/apikey.go — 消費方定義自己需要的介面
package service

// WorkerManager 管理用戶 Worker 的生命週期（由 lending.Service 隱式實作）
type WorkerManager interface {
    StartWorker(ctx context.Context, userID string, cfg domain.StrategyConfig) error
    StopWorker(ctx context.Context, userID string) error
}

type APIKeyService struct {
    repo       repository.APIKeyRepository
    configRepo repository.ConfigRepository
    cipher     *crypto.AES
    bfx        *bitfinex.Client
    workers    WorkerManager   // fx 自動注入 *lending.Service
}

func NewAPIKeyService(repo repository.APIKeyRepository, configRepo repository.ConfigRepository,
    cipher *crypto.AES, bfx *bitfinex.Client, workers WorkerManager) *APIKeyService { ... }
```

```go
// service/config.go — 同樣的模式
package service

// ConfigReloader 通知 Worker 熱載入新參數（由 lending.Service 隱式實作）
type ConfigReloader interface {
    ReloadConfig(ctx context.Context, userID string, cfg domain.StrategyConfig) error
}

type ConfigService struct {
    repo     repository.ConfigRepository
    reloader ConfigReloader  // fx 自動注入 *lending.Service
}
```

> **設計說明**：`service/` 不 import `lending/`，只定義自己需要的小 interface。`lending.Service` 的 `StartWorker`、`StopWorker`、`ReloadConfig` 方法簽名剛好符合這些 interface（Go 隱式實作）。fx 在啟動時自動將 `*lending.Service` 注入為 `WorkerManager` 和 `ConfigReloader`，無需手動綁定。

### 4.3 Application 層 — 放貸引擎 (`lending/`)

**職責**：V10 策略設計文件的全部實作。獨立生命週期，由 `main.go` 啟動，不被 HTTP 請求驅動。

**核心檔案**：

`lending/service.go`：引擎頂層編排器。
```go
package lending

// WorkerDepsFactory 為每個新 Worker 建立完整的依賴（Strategy, DataFetcher, OfferExecutor）。
// 由 lending/factory.go 的 DepsFactory 實作，避免 Service 直接 import strategy/ 或 execution/。
type WorkerDepsFactory interface {
    BuildWorkerDeps(userID string, snapshotCh <-chan *domain.MarketSnapshot) worker.Deps
}

type Service struct {
    pool        *worker.Pool
    quota       *quota.Allocator
    depsFactory WorkerDepsFactory
    config      Config
    snapshotChs map[string]chan *domain.MarketSnapshot // per-user snapshot channels
}

func NewService(depsFactory WorkerDepsFactory, cfg Config) *Service { ... }

// Start 啟動 quota refill + Worker Pool，阻塞直到 ctx 取消
func (s *Service) Start(ctx context.Context) error { ... }

// Stop 優雅關閉：停止所有 Worker → 關閉 snapshot channels → 停止 quota refill
func (s *Service) Stop(ctx context.Context) error { ... }

// Ready 回傳一個在初始化完成後 close 的 channel（用於啟動同步）
func (s *Service) Ready() <-chan struct{} { ... }

// StartWorker 新增一個用戶的 Worker（由 service/apikey.go 透過 WorkerManager 呼叫）
func (s *Service) StartWorker(ctx context.Context, userID string, cfg domain.StrategyConfig) error { ... }

// StopWorker 停止一個用戶的 Worker
func (s *Service) StopWorker(ctx context.Context, userID string) error { ... }

// ReloadConfig 通知 Worker 熱載入新參數（由 service/config.go 透過 ConfigReloader 呼叫）
func (s *Service) ReloadConfig(ctx context.Context, userID string, cfg domain.StrategyConfig) error { ... }

// Status 回傳引擎運行狀態（用於健康檢查）
func (s *Service) Status() domain.EngineStatus { ... }

// BroadcastSnapshot 廣播 MarketSnapshot 到所有 Worker（由 main.go 的 snapshot 轉發 goroutine 呼叫）
func (s *Service) BroadcastSnapshot(snapshot *domain.MarketSnapshot) { ... }
```

`lending/factory.go`：WorkerDepsFactory 具體實作。
```go
// DepsFactory 持有共享基礎設施（Bitfinex client、加密、API key repo、執行紀錄 repo），
// 為每個 Worker 建立：
// - Strategy: strategy.PricingStrategy
// - DataFetcher: 透過 Bitfinex REST 拉取 wallet + offers + credits
// - OfferExecutor: 包裝 execution.OfferExecutor 為 worker.OfferExecutor adapter
type DepsFactory struct { ... }
func NewDepsFactory(client, cipher, apiKeyRepo, execRepo) *DepsFactory { ... }
```

**Consumer-Side Interface 設計**：

Go 慣例是 interface 由**消費方**定義，而非實作方。這避免了 parent/child package 間的循環依賴（parent `lending/` import child `lending/signal/`，child 不可反向 import parent）。各子 package 的 interface 定義在使用它們的地方：

```go
// marketfeed/service.go — 消費方定義它需要的信號源介面
package marketfeed

type SignalSource interface {
    Name() string
    Compute(data *domain.RawMarketData) domain.SignalValue
}

// worker/worker.go — 消費方定義它需要的策略與執行介面
package worker

type Strategy interface {
    Apply(ctx *domain.DecisionContext) *domain.DecisionResult
}

type DataFetcher interface {
    FetchUserData(ctx context.Context, userID string) (*UserData, error)
}

type OfferExecutor interface {
    ExecuteDecision(ctx context.Context, userID string, decision *domain.DecisionResult) (*ExecutionSummary, error)
}

type Deps struct {
    Strategy   Strategy
    Fetcher    DataFetcher
    Executor   OfferExecutor
    SnapshotCh <-chan *domain.MarketSnapshot
}
```

`signal/`、`strategy/`、`execution/` 只依賴 `domain/`，自然滿足上述 interface（Go 的隱式實作），無需 import 消費方 package。由 `lending/factory.go` 的 `DepsFactory` 負責建立 adapter 並注入具體實作。

**子 package 職責**：

| Package | 職責 | V10 對應章節 |
| :--- | :--- | :--- |
| `marketfeed/` | 共享市場數據主循環，Phase 0-3 | §10.2 |
| `signal/` | 6 個信號源計算 + MDC 加權 | §2, §3.3-3.6, §4.4-4.5 |
| `orderbook/` | Order Book 快照分析 | §3.1-3.2, §3.7-3.8 |
| `strategy/` | 13 個策略模組 | §4.2-4.14, §5 |
| `execution/` | 掛單 / 撤單 / 債權管理 | §7.1-7.2 |
| `worker/` | Per-User Worker 生命週期 | §10.3 |
| `quota/` | 全局 API 配額分配 | §10.5 |

**依賴規則**：
- `signal/`、`orderbook/`、`strategy/` 是純計算模組，**只依賴 `domain/`**，不依賴 I/O，不 import 任何 sibling 或 parent package。
- `marketfeed/` 依賴 `bitfinex/`（拉取數據）和 `signal/`、`orderbook/`（計算），自行定義 `SignalSource` interface。
- `execution/` 依賴 `bitfinex/`（操作掛單）和 `repository/`（寫紀錄）。
- `worker/` 透過自行定義的 `Strategy`、`Executor` interface 做決策與執行，不直接 import `strategy/` 或 `execution/`。具體實作由 `lending/factory.go` 的 `DepsFactory` 建立並注入。
- **禁止循環依賴**：子 package 絕不 import parent `lending/`，子 package 之間不互相 import。Worker 依賴組裝由 `lending/factory.go` 完成，頂層協調由 `lending/service.go` 完成。

### 4.4 Domain 層 (`domain/`)

**職責**：定義所有業務實體和值物件。純 struct + enum + 驗證方法。零外部依賴。

**規範**：
- 不 import 任何 `internal/` 下的其他 package。
- 不 import 第三方庫（database driver、HTTP framework 等）。
- 可以包含業務驗證邏輯（例如 `StrategyConfig.Validate() error`）。

**核心型別**：

```go
// domain/snapshot.go
type MarketSnapshot struct {
    Symbol             string
    FRR                float64       // Flash Return Rate (daily) from ticker
    MDC                MDCResult
    Regime             RegimeType
    RegimeParams       RegimeParams
    Signals            []SignalValue
    OrderBook          OrderBookSummary
    WallPositions      []WallPosition
    HiddenRatio        float64 // estimated hidden order ratio
    CompetitorActivity float64 // 0-1 composite competitor activity score
    FlashFreeze        bool    // true if flash crash detected, trading paused
    Timestamp          time.Time
}

// domain/config.go
type StrategyConfig struct {
    Currency  string       `json:"currency"`
    Amount    AmountConfig `json:"amount"`  // Min/Max 掛單金額
    Rate      RateConfig   `json:"rate"`    // Min/Max 利率
    Period    PeriodConfig `json:"period"`  // Min/Max 天數
    AutoRenew bool         `json:"auto_renew"`
}

// domain/decision.go — 策略決策層 I/O
type DecisionContext struct {
    Snapshot      *MarketSnapshot
    Config        *StrategyConfig
    ActiveOffers  []FundingOffer
    ActiveCredits []FundingCredit
    Available     float64 // available balance in funding wallet
    Currency      string  // e.g. "fUSD"
}

type DecisionResult struct {
    Offers       []OfferDecision // recommended offers to place
    Cancels      []int64         // offer IDs to cancel
    RenewCredits []int64         // credit IDs to renew
    Reason       string          // human-readable decision reason
}

type OfferDecision struct {
    Amount float64
    Rate   float64
    Period int
}
```

### 4.5 Infrastructure 層

#### `infra/`

**職責**：外部服務的**連線建立與生命週期管理**。只負責「連上去」，不負責「怎麼用」。

```go
// infra/di.go
package infra

var Module = fx.Module("infra",
    fx.Provide(NewPostgresPool),  // → *pgxpool.Pool
    fx.Provide(NewRedisClient),   // → *redis.Client
)
```

```go
// infra/postgres.go
package infra

func NewPostgresPool(lc fx.Lifecycle, cfg appconfig.Config, log *zap.Logger) (*pgxpool.Pool, error) {
    pool, err := pgxpool.New(context.Background(), cfg.DatabaseURL)
    if err != nil {
        return nil, err
    }
    lc.Append(fx.Hook{
        OnStop: func(ctx context.Context) error {
            pool.Close()
            return nil
        },
    })
    return pool, nil
}
```

**規範**：
- 每個檔案只做一件事：建立連線 + 註冊 `fx.Lifecycle` 的關閉 hook。
- 暴露的是**具體型別**（`*pgxpool.Pool`、`*redis.Client`），不需要額外包 interface — 這些都是第三方庫的標準型別，DAO 層直接依賴即可。
- `handler/health.go` 直接注入 `*pgxpool.Pool`、`*redis.Client`、`EngineHealthProvider` 做健康檢查，回應包含 Postgres、Redis 和 Lending Engine 狀態。`EngineHealthProvider` 回傳 `domain.EngineStatus`，handler 不 import `lending/`。
- `bitfinex/`、`notification/`、`crypto/` **不放進 `infra/`** — 它們不是純連線初始化，各自有業務邏輯，作為獨立 package 更清晰。

#### `repository/`

**結構**：參考 SosReaderServer 的 `repositories/` 模式，所有資料存取（PostgreSQL + Redis）集中在 `repository/` 下，各子目錄用 `di.go` 提供 `fx.Module`，頂層 `di.go` 聚合。DAO 透過 fx 注入 `infra/` 提供的連線實例。

```go
// repository/di.go — 聚合所有資料存取的 fx Module
package repository

var Module = fx.Module("repository",
    postgres.Module,
    redis.Module,
)
```

**規範**：
- `di.go` 聚合子 module，`interfaces.go` 集中定義所有 Repository interface。
- `postgres/di.go` 註冊所有 PostgreSQL repo 的 fx provider。
- `postgres/query/` 放手寫的 SQL 查詢檔（sqlc 輸入）。
- `postgres/sqlc/` 放 sqlc 自動產生的 Go 檔案（**勿手動修改**，由 `sqlc generate` 產生）。
- `postgres/*.go` 是手寫的薄包裝層，負責：
  - 持有 `sqlc.Queries` 實例
  - 實作 `repository.XxxRepository` interface
  - 將 `sqlc/models.go` 的 DB row struct 轉換為 `domain/` 的業務型別
- `redis/di.go` 註冊所有 Redis DAO 的 fx provider。
- `redis/*.go` 各自封裝不同用途的 Redis 操作（Session、快取、Pub/Sub）。
- 未來如需更換資料庫，新增子目錄（如 `mysql/`）即可，不影響上層。

```go
// repository/interfaces.go — 手寫，定義業務契約（使用 domain 型別）
package repository

type UserRepository interface {
    Create(ctx context.Context, email, passwordHash string) (*domain.User, error)
    GetByID(ctx context.Context, id string) (*domain.User, error)
    GetByEmail(ctx context.Context, email string) (*domain.User, error)
    UpdatePassword(ctx context.Context, id, passwordHash string) error
}

type APIKeyRepository interface {
    Create(ctx context.Context, userID, label, apiKey string, encryptedSecret []byte, exchangeStatus string) (*domain.APIKey, error)
    GetByID(ctx context.Context, id string) (*domain.APIKey, []byte, error)
    GetByUserID(ctx context.Context, userID string) (*domain.APIKey, []byte, error)
    ListVerified(ctx context.Context) ([]domain.APIKey, [][]byte, error)
    UpdateExchangeStatus(ctx context.Context, id, status string) error
    Delete(ctx context.Context, id, userID string) error
}

type ConfigRepository interface {
    Upsert(ctx context.Context, userID string, configJSON []byte) (*domain.UserConfig, error)
    GetByUserID(ctx context.Context, userID string) (*domain.UserConfig, error)
    DeleteByUserID(ctx context.Context, userID string) error
}

type ExecutionRepository interface {
    Create(ctx context.Context, record *domain.ExecutionRecord) (*domain.ExecutionRecord, error)
    ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.ExecutionRecord, error)
    ListByUserPaginated(ctx context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.ExecutionRecord, error)
}

type BillingRepository interface {
    Create(ctx context.Context, record *domain.BillingRecord) (*domain.BillingRecord, error)
    ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.BillingRecord, error)
    ListByUserPaginated(ctx context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.BillingRecord, error)
}

type SnapshotCache interface {
    Set(ctx context.Context, symbol string, snapshot *domain.MarketSnapshot, ttl time.Duration) error
    Get(ctx context.Context, symbol string) (*domain.MarketSnapshot, error)
    Delete(ctx context.Context, symbol string) error
}

type SnapshotPubSub interface {
    Publish(ctx context.Context, snapshot *domain.MarketSnapshot) error
    Subscribe(ctx context.Context) (<-chan *domain.MarketSnapshot, error)
    Close() error
}
```

```go
// postgres/user.go — 手寫包裝層，橋接 sqlc 生成碼與 repository interface
package postgres

type UserRepo struct {
    q *sqlc.Queries
}

func (r *UserRepo) GetByID(ctx context.Context, id string) (*domain.User, error) {
    row, err := r.q.GetUserByID(ctx, id)
    if err != nil {
        return nil, err
    }
    return toDomainUser(row), nil  // sqlc model → domain 型別轉換
}
```

**sqlc 配置範例**：
```yaml
# sqlc.yaml
version: "2"
sql:
  - engine: "postgresql"
    queries: "internal/repository/postgres/query"
    schema: "migrations"
    gen:
      go:
        package: "sqlc"
        out: "internal/repository/postgres/sqlc"
        sql_package: "pgx/v5"
        emit_interface: true
```

> **為什麼 sqlc 生成碼和手寫包裝層分開？** sqlc 生成的 `models.go` 是 DB row 的直接映射（含 `sql.NullString` 等 DB 型別），而 `domain/` 使用業務語義的型別。包裝層負責這個轉換，讓上層（`service/`、`lending/`）只接觸 `domain/` 型別，不感知 DB 細節。同時 `repository.XxxRepository` interface 的方法簽名使用 `domain/` 型別，sqlc 生成的 `Querier` interface 使用 DB 型別，兩者不混淆。

#### `bitfinex/`

**職責**：自製 Bitfinex API 封裝（HMAC-SHA384 認證），隔離交易所 API 細節。

**規範**：
- 上層不直接呼叫 Bitfinex HTTP API，只透過這個 package 存取。
- `client.go` 封裝 REST API v2（查詢餘額、驗證權限、提交掛單），含 HMAC-SHA384 簽名。
- `auth.go` 提供認證簽名邏輯（nonce + payload + signature）。
- `ws.go` 管理 WebSocket 連接（公開頻道 + 認證頻道），含自動重連 + heartbeat + DMS。
- `ws_types.go` 定義 WebSocket 訊息型別。

---

## 5. 兩條呼叫鏈

### 5.1 CRUD 請求（HTTP 驅動）

以「用戶修改策略參數」為例：

```
[Frontend] POST /api/v1/configs
    │
    ▼
handler/router.go → middleware/jwt.go → handler/config.go
    │                    解析 + 驗證 request body
    ▼
service/config.go
    │  1. 呼叫 domain.StrategyConfig.Validate() 驗證參數合理性
    │  2. 呼叫 repository.ConfigRepository.Upsert() 持久化
    │  3. 呼叫 ConfigReloader.ReloadConfig(ctx, userID, newConfig) 通知 Worker
    │     （ConfigReloader 由 fx 注入，實際為 *lending.Service）
    ▼
lending/worker/worker.go
    │  下一個心跳載入新參數，不需重啟
    ▼
handler/config.go → 回傳 200 OK
```

### 5.2 放貸引擎（自主運行）

每次心跳的完整流程：

```
lending/service.go 啟動
    │
    ├── marketfeed/service.go（共享層，跑在獨立 goroutine）
    │       │
    │       ├── Phase 0: 前置檢查（冷啟動、閃崩凍結、API 預算）
    │       ├── Phase 1: bitfinex/ws.go 拉取公開數據
    │       ├── Phase 1.5: flashcrash.go 閃崩偵測
    │       ├── Phase 2: signal/*.go 計算 6 個信號 → mdc.go 加權
    │       ├── Phase 3: regime.go 體制判定
    │       └── snapshot.go 組裝 MarketSnapshot → 廣播（Go channel）
    │
    └── worker/pool.go 管理所有用戶的 Worker
            │
            ├── worker/worker.go [User_A]（獨立 goroutine）
            │       │  收到 MarketSnapshot
            │       ├── Phase 1 (私有): bitfinex/client.go 拉取錢包 + 掛單
            │       ├── Phase 4: strategy/*.go 利率/天數決策
            │       ├── Phase 5: execution/*.go 掛單執行
            │       ├── Phase 6: credit.go Auto-Renew 管理
            │       └── Phase 7: repository/postgres/execution.go 寫紀錄
            │
            ├── worker/worker.go [User_B]（獨立 goroutine）
            │       └── ...
            │
            └── quota/allocator.go 分配 API 配額
```

---

## 6. `main.go` 啟動流程（fx）

```go
// cmd/server/main.go
func main() {
    fx.New(
        // ── Infrastructure（連線初始化）──
        appconfig.Module,           // Config 載入
        infra.Module,               // *pgxpool.Pool + *redis.Client
        repository.Module,          // 聚合 postgres 所有 DAO

        fx.Provide(newLogger),
        fx.Provide(func(cfg appconfig.Config) *auth.JWTManager { ... }),
        fx.Provide(func(cfg appconfig.Config) (*crypto.AES, error) { ... }),
        fx.Provide(func() *bitfinex.Client { ... }),
        fx.Provide(func(cfg appconfig.Config) notification.Notifier {
            return notification.NewResendNotifier(cfg.ResendAPIKey, cfg.NotificationFromEmail)
        }),

        // ── Lending Engine ──
        fx.Provide(lending.NewDepsFactory),
        fx.Provide(newLendingService),    // 同時提供 *lending.Service + WorkerManager + ConfigReloader
        fx.Provide(newMarketFeedService), // marketfeed.Service with 5 signal sources

        // ── Service 層 ──
        fx.Provide(service.NewUserService),
        fx.Provide(service.NewAPIKeyService),   // 注入 WorkerManager
        fx.Provide(service.NewConfigService),   // 注入 ConfigReloader
        fx.Provide(service.NewDashboardService),
        fx.Provide(service.NewEarningsService),
        fx.Provide(service.NewExecutionService),
        fx.Provide(service.NewBillingService),

        // ── Transport 層 ──
        fx.Provide(handler.NewAuthHandler),
        // ... (all handlers)
        fx.Provide(func(svc *lending.Service) handler.EngineHealthProvider { return svc }),
        fx.Provide(handler.NewHealthHandler),
        fx.Provide(handler.NewRouter),

        // ── 生命週期管理 ──
        fx.Invoke(startServer),
        fx.Invoke(startLendingEngine),  // marketfeed → lending.Service → snapshot 轉發 → 既有用戶恢復
    ).Run()
}

// newLendingService 同時回傳 *lending.Service 和兩個 interface，
// 讓 fx 自動注入 service.WorkerManager + service.ConfigReloader
func newLendingService(factory *lending.DepsFactory) (*lending.Service, service.WorkerManager, service.ConfigReloader) {
    svc := lending.NewService(factory, lending.DefaultConfig())
    return svc, svc, svc
}
```

**fx 的優勢**：
- **自動 wire**：不需要手動管理建構順序。fx 根據建構函式的參數型別自動解析依賴圖。
- **interface 自動匹配**：`*lending.Service` 實作了 `service.WorkerManager` 和 `service.ConfigReloader`，fx 自動注入，不需要 callback 或 `SetXxx`。
- **生命週期管理**：`fx.Lifecycle` 統一管理啟動/關閉順序（OnStop 按 LIFO 順序執行），取代手動 signal handling。
- **錯誤檢測**：啟動時若有依賴缺失或循環依賴，fx 會直接報錯並拒絕啟動，而非 runtime panic。

---

## 7. 依賴關係圖

```
                          cmd/server/main.go
                                 │
                     ┌───────────┼───────────┐
                     ▼           ▼           ▼
                handler/ ───→ service/    lending/
                     │           │           │
                     │           │     ┌─────┼──────────────────────┐
                     │           │     │     │                      │
                     │           │     ▼     ▼                      ▼
                     │           │  market  worker/    signal/  orderbook/  strategy/
                     │           │  feed/   execution/    │        │          │
                     │           │     │       │       (純計算，只依賴 domain/)
                     │           │     │       │          │        │          │
                     │           ▼     ▼       ▼          │        │          │
                     │     ┌───────────────────────┐      │        │          │
                     │     │ repository/  bitfinex/ │      │        │          │
                     │     └─────┬─────────────────┘      │        │          │
                     │           │          │              │        │          │
                     │           │          └──────────────┴────────┴──────────┘
                     │           │                         │
                     ▼           ▼                         ▼
                infra/       domain/
          (pgxpool.Pool,  （零外部依賴）
           redis.Client)
                     │
                     ▼
               appconfig/
```

**箭頭方向 = import 方向。所有層最終依賴 `domain/`，`domain/` 不依賴任何人。**
**Infrastructure 層（repository/, bitfinex/）依賴 `domain/` 做型別轉換，但不被 `domain/` 反向依賴。**

**禁止的依賴**：
- `domain/` → 任何其他 package ❌
- `repository/` → `service/` 或 `lending/` ❌（上層依賴下層，不可反向）
- `signal/` → `bitfinex/` ❌（信號計算是純邏輯，不做 I/O）
- `strategy/` → `bitfinex/` ❌（同上）
- `handler/` → `lending/` ❌（`handler/` 透過 `EngineHealthProvider` interface 取得引擎狀態，fx 自動注入 `*lending.Service`，無需 import）
- `service/` → `lending/` ❌（`service/` 定義自己的小 interface 如 `WorkerManager`，fx 自動注入 `*lending.Service`，無需 import）

---

## 8. Package 命名對照表

| Package | import path | 用途 | 對應 V10 章節 |
| :--- | :--- | :--- | :--- |
| `auth` | `internal/auth` | JWT 簽發 + 驗證（RS256） | §10.4 |
| `handler` | `internal/handler` | HTTP 請求處理 + 路由註冊 | §10.8 |
| `middleware` | `internal/middleware` | JWT, Rate Limit | §10.8 |
| `service` | `internal/service` | CRUD 業務編排 | §10.4, 10.6, 10.7 |
| `lending` | `internal/lending` | 放貸引擎入口 | §10.1 |
| `marketfeed` | `internal/lending/marketfeed` | 共享市場數據 | §10.2 |
| `signal` | `internal/lending/signal` | 信號計算 | §2, §3.3-3.6, §4.4-4.5 |
| `orderbook` | `internal/lending/orderbook` | 掛單簿分析 | §3.1-3.2, §3.7-3.8 |
| `strategy` | `internal/lending/strategy` | 策略決策 | §4.2-4.14, §5 |
| `execution` | `internal/lending/execution` | 掛單執行 | §7.1-7.2 |
| `worker` | `internal/lending/worker` | Worker 管理 | §10.3 |
| `quota` | `internal/lending/quota` | API 配額 | §10.5 |
| `domain` | `internal/domain` | 領域模型 | 附錄 B |
| `infra` | `internal/infra` | DB/Redis 連線初始化 + fx Lifecycle | — |
| `repository` | `internal/repository` | 資料存取介面 + fx Module 聚合 | §10.10 |
| `postgres` | `internal/repository/postgres` | PostgreSQL DAO（手寫包裝層 + fx Module） | §10.10 |
| `sqlc` | `internal/repository/postgres/sqlc` | sqlc 自動產生的 query 程式碼（勿手改） | §10.10 |
| `redis` | `internal/repository/redis` | Redis DAO（Session、快取、Pub/Sub） | §10.2 |
| `bitfinex` | `internal/bitfinex` | 交易所 API | §6.1-6.2 |
| `notification` | `internal/notification` | 告警通知 | §10.9 |
| `crypto` | `internal/crypto` | 加解密 | §10.4 |
| `appconfig` | `internal/appconfig` | 應用配置（環境變數 / YAML） | — |

---

## 9. 關鍵設計決策記錄

### 9.1 為什麼不用 API Gateway

CRUD API 和放貸引擎在同一個 Go binary 中運行。拆成兩個服務會增加 gRPC 通訊延遲、部署複雜度和服務間依賴管理。初期 < 500 用戶的規模下，單體是正確的選擇。內部透過 Go interface 解耦，未來需要拆分時改為 gRPC 呼叫即可。

### 9.2 為什麼不用 controller-usecase-service-repository 四層

usecase 和 service 職責重疊。放貸引擎是 event-driven 的長駐系統，不是 request-response，強套四層會很彆扭。Go 社區主流是 handler → service → repository 三層。

### 9.3 為什麼 `lending/` 有子 package 而 `service/` 沒有

`service/` 的 CRUD 邏輯簡單，每個檔案 100-200 行，不需要進一步拆分。`lending/` 包含 V10 的全部策略邏輯（數十個演算法模組），如果不拆分，單個 package 會膨脹到幾千行，違反 Go 的「小而聚焦的 package」慣例。

### 9.4 為什麼 `signal/` 和 `strategy/` 不做 I/O

純計算模組與 I/O 解耦後：可以用單元測試直接驗證演算法正確性（不需要 mock WebSocket）；可以用歷史數據做回測（直接傳入 `domain.RawMarketData`）；`marketfeed/` 和 `worker/` 負責 I/O，`signal/` 和 `strategy/` 負責計算，職責清晰。

### 9.5 為什麼不在 `lending/` 頂層定義 interface

Go 不允許 parent package 與 child package 互相 import。如果在 `lending/interfaces.go` 定義 `SignalSource`，則 `lending/signal/` 需要 import `lending/` 取得 interface 定義，而 `lending/service.go` 又需要 import `lending/signal/`，形成循環依賴。

解法是遵循 Go 的 **consumer-side interface** 慣例：interface 由消費方定義。`marketfeed/` 定義它需要的 `SignalSource`，`worker/` 定義它需要的 `Strategy` 和 `Executor`。實作方（`signal/`、`strategy/`、`execution/`）只依賴 `domain/`，透過 Go 的隱式介面滿足自然符合消費方的 interface，無需 import 任何 sibling 或 parent package。

### 9.6 為什麼用 fx 而非手動 wire

專案有兩條獨立的執行鏈（HTTP CRUD + Lending Engine），且 `service/` 需要透過 interface 與 `lending/` 溝通。手動 wire 需要小心控制建構順序，且 callback / `SetXxx` 模式不易維護。fx 的優勢：
- **自動依賴解析**：不需要手動排序建構順序，fx 根據型別自動 wire。
- **interface 自動匹配**：`*lending.Service` 實作 `service.WorkerManager`，fx 自動注入，不需要顯式轉型或 callback。
- **生命週期統一管理**：HTTP server 和 Lending Engine 的 start/stop 都註冊在 `fx.Lifecycle`，關閉順序由 fx 以 LIFO 保證。
- **啟動時錯誤檢測**：依賴缺失或循環時 fx 直接報錯，不會 runtime panic。

代價是引入了一個第三方依賴，但 fx 是 Uber 開源的成熟專案，Go 社區廣泛使用。

### 9.7 Context.Context 使用原則

嚴格遵守 **「I/O 與阻塞才需要 Context，純計算絕不使用」** 的原則：

| 層級 | context.Context | 原因 |
| :--- | :--- | :--- |
| `handler/`, `middleware/` | ✅ 使用 | 產生並往下傳遞 Request Context |
| `service/` | ✅ 使用 | 編排 I/O 操作，傳遞 Context |
| `repository/`, `bitfinex/` | ✅ 使用 | DB、Redis、HTTP 等 I/O 需要超時控制 |
| `lending/worker/`, `lending/service.go` | ✅ 使用 | Worker 主迴圈是長駐阻塞操作，需要 cancel 機制 |
| `domain/` | ❌ 禁用 | 純型別定義，零 I/O |
| `lending/signal/` | ❌ 禁用 | 純數學計算（均線、動量、閃崩偵測），微秒內完成 |
| `lending/strategy/` | ❌ 禁用 | 純決策邏輯，給定輸入直接算出結果 |
| `lending/orderbook/` | ❌ 禁用 | 純資料結構操作（Order Book 解析） |

禁用 Context 的好處：極致效能（無指標傳遞開銷）、極致可測試性（直接傳假資料秒測，不需 mock I/O）。

### 9.8 為什麼 Bitfinex Client 不做重試（Retry）

Circuit breaker + rate limiter 已經提供足夠的 resilience，**刻意不加 retry 機制**。原因：

1. **金融 API 重試很危險**：`SubmitFundingOffer` 如果因 5xx/timeout 重試，可能造成**重複掛單**。Bitfinex 沒有提供 idempotency key，無法安全重試寫入操作。
2. **Circuit breaker 已防級聯失敗**：5 次連續失敗 → 熔斷 15 秒，不需要 retry 來「再試一次」。
3. **Bitfinex 回傳格式特殊**：API 錯誤以 HTTP 200 + `["error", code, msg]` 回傳，HTTP status code 檢查（429/5xx）對 Bitfinex API 意義有限。

如果未來需要對**讀取操作**（`GetFundingBalance`、`GetActiveFundingOffers`）加 retry，應只對讀取方法做，寫入操作（`SubmitFundingOffer`、`CancelFundingOffer`）永遠不重試。

### 9.9 為什麼 Repository interface 放在 `repository/` 而不是 `domain/`

Go 慣例是在「使用方」定義 interface（consumer-side interface）。但 Repository interface 被多個使用方（`service/`、`lending/execution/`）共用，放在 `domain/` 會讓 domain 對 `context`、`time` 以外的標準庫產生隱式依賴。放在 `repository/` package 頂層是務實的折衷——它是 interface 的「家」，`postgres/` 子目錄是實作。

---

## 10. 擴展路徑

### 10.1 單機 → 多機（> 500 用戶）

- `lending/marketfeed/` 的 MarketSnapshot 廣播從 Go channel 切換到 Redis Pub/Sub。
- Strategy Engine 可以部署多台，每台承載一批 Worker。
- `quota/allocator.go` 改為從 Redis 讀取全局配額狀態（分散式配額）。
- `repository/` 不需要改動（已經是外部服務）。

### 10.2 單體 → 微服務（> 2,000 用戶）

拆分邊界已經由 package 結構定義好：

| 微服務 | 包含的 package | 說明 |
| :--- | :--- | :--- |
| API Service | `handler/`, `middleware/`, `service/`, `domain/`, `repository/` | HTTP + CRUD |
| Lending Engine | `lending/`, `domain/`, `repository/`, `bitfinex/` | 策略 + 執行 |
| Billing Service | `billing/`, `domain/`, `repository/` | 計費 |
| Notification Service | `notification/` | 告警 |

package 間目前透過 Go interface 呼叫的地方，改為 gRPC 即可。`domain/` 的 struct 直接映射為 protobuf message。

---

## 11. 部署架構

### 部署平台

| 元件 | 平台 | 說明 |
| :--- | :--- | :--- |
| Frontend | Vercel | Next.js 官方平台，自動部署 |
| Backend | Koyeb | Docker 部署，支援 WebSocket 長連線 |
| Database | Neon | Serverless PostgreSQL，自動擴縮 |
| Cache | Upstash | Serverless Redis，按用量計費 |

### Koyeb 部署

透過 Git 整合自動部署：連結 GitHub repo，push 到 `main` 時自動 build Docker image + deploy。

```dockerfile
# backend/Dockerfile
FROM golang:1.25-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o server ./cmd/server/

FROM alpine:3.21
RUN apk add --no-cache ca-certificates tzdata
WORKDIR /app
COPY --from=builder /app/server .
EXPOSE 8080
CMD ["./server"]
```

### Graceful Shutdown

Koyeb 透過 SIGTERM 終止應用。後端必須正確處理，確保 WebSocket 連線和 Worker goroutine 安全關閉：

```go
// cmd/server/main.go 中由 fx 統一管理生命週期
// fx.Lifecycle 會在收到 SIGTERM 時按 LIFO 順序執行 OnStop：
// 1. 停止 HTTP server（停止接收新請求）
// 2. 關閉所有 Worker goroutine（停止放貸操作）
// 3. 關閉 WebSocket 連線（通知前端重連）
// 4. 關閉 DB / Redis 連線
```

### 環境變數

在 Koyeb Dashboard > Service > Environment Variables 設定：

```bash
# Server
PORT=8080
ENVIRONMENT=development  # development | production

# Database (Neon)
DATABASE_URL="postgresql://user:pass@ep-xxx.region.aws.neon.tech/dbname?sslmode=require"

# Cache (Upstash)
REDIS_URL="rediss://default:xxx@xxx.upstash.io:6379"

# Auth
JWT_PRIVATE_KEY=...    # RS256 private key (PEM)
JWT_PUBLIC_KEY=...     # RS256 public key (PEM)
AES_KEY=...            # API Key 加密用 256-bit hex key (32 bytes)

# Notification (Resend)
RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
NOTIFICATION_FROM_EMAIL=noreply@bfx-funding.bot

# Frontend
FRONTEND_URL=https://app.example.com   # CORS 白名單
```

> **Neon 連線注意**：Neon 要求 `sslmode=require`。放貸引擎 24/7 常駐會持續查詢 DB，Neon compute 基本不會 auto-suspend，需注意持續計費。

> **Upstash Redis 注意**：Upstash 使用 TLS 連線，URL scheme 為 `rediss://`（雙 s）。Go 的 `go-redis` v9+ 原生支援。
