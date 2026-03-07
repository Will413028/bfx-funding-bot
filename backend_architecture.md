# Bitfinex 自動放貸 SaaS 平台：後端架構設計文件

---

## 1. 技術棧總覽

| 層級 | 技術 | 說明 |
| :--- | :--- | :--- |
| 語言 | Go 1.24+ | 單一二進位部署，goroutine 驅動 |
| HTTP 框架 | Gin / Echo | handler + middleware |
| 資料庫 | PostgreSQL 18 | 多租戶持久化，Row-Level Security |
| 快取 | Redis 8 | Session、MarketSnapshot 快取、Pub/Sub |
| Bitfinex API | bitfinex-api-go/v2 | 官方 Go SDK，REST + WebSocket |
| 加密 | AES-256-GCM | API Key 加密儲存 |
| 認證 | JWT (RS256) | Stateless Token |
| 部署 | Docker + Docker Compose | 初期單機，後期可拆分 |
| 監控 | Prometheus + Grafana | 指標收集 + 儀表板 |
| DB Migration | Atlas | 宣告式 Schema + 版本化 Migration |
| DB Query | sqlc | SQL → Type-Safe Go Code 生成 |
| DI 框架 | go.uber.org/fx | 建構式依賴注入 + 生命週期管理 |

---

## 2. 分層架構

```
┌──────────────────────────────────────────────────────┐
│                    Transport 層                       │
│  api/handler/   → HTTP 請求處理、參數驗證、回應格式  │
│  api/middleware/ → JWT、Rate Limit                     │
├──────────────────────────────────────────────────────┤
│                   Application 層                      │
│  service/  → CRUD 業務編排（用戶、API Key、計費）    │
│  lending/  → 放貸引擎（獨立生命週期，非 HTTP 驅動） │
├──────────────────────────────────────────────────────┤
│                     Domain 層                         │
│  domain/  → 純型別定義 + 業務規則（零外部依賴）      │
├──────────────────────────────────────────────────────┤
│                 Infrastructure 層                     │
│  repository/ → PostgreSQL 資料存取                    │
│  cache/      → Redis 快取 + Pub/Sub                  │
│  bitfinex/   → 交易所 API 封裝                       │
│  notification/ → Email 通知                          │
│  crypto/     → 加解密                                │
└──────────────────────────────────────────────────────┘
```

**核心設計原則**：依賴方向嚴格由上往下。Domain 層不依賴任何其他層。上層透過 interface 依賴下層，不直接依賴具體實作。

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
│   │
│   ├── api/
│   │   ├── handler/
│   │   │   ├── auth.go                  # 註冊 / 登入 / JWT 簽發
│   │   │   ├── user.go                  # 用戶 CRUD
│   │   │   ├── apikey.go                # API Key 管理
│   │   │   ├── config.go                # 策略參數 CRUD
│   │   │   ├── billing.go               # 帳單查詢
│   │   │   ├── dashboard.go             # WebSocket 即時推送
│   │   │   └── health.go                # 健康檢查 + Readiness
│   │   ├── middleware/
│   │   │   ├── jwt.go                   # JWT 驗證
│   │   │   └── ratelimit.go             # 請求頻率限制
│   │   └── router.go                    # 路由註冊
│   │
│   │   # ── Application 層：CRUD 業務 ──
│   │
│   ├── service/
│   │   ├── user.go                      # 用戶註冊 / 登入 / 管理
│   │   ├── apikey.go                    # API Key CRUD + 加密 + 權限驗證
│   │   ├── config.go                    # 策略參數管理 + 通知 Worker 熱載入
│   │   └── billing.go                   # 計費計算 + 帳單生成
│   │
│   │   # ── Application 層：放貸引擎 ──
│   │
│   ├── lending/
│   │   ├── service.go                   # 引擎入口：啟動共享層 + Worker Pool
│   │   │
│   │   ├── marketfeed/                  # 共享市場數據服務（Phase 0-3）
│   │   │   ├── service.go               #   主循環：數據拉取 → 信號計算 → 廣播
│   │   │   ├── snapshot.go              #   MarketSnapshot 組裝
│   │   │   └── flashcrash.go            #   閃崩偵測（Phase 1.5）
│   │   │
│   │   ├── signal/                      # 信號計算模組
│   │   │   ├── mdc.go                   #   MDC 計算 + 衰減 + 復甦平滑
│   │   │   ├── book.go                  #   Order Book 消耗速度
│   │   │   ├── liquidation.go           #   清算瀑布偵測
│   │   │   ├── momentum.go              #   雙速 VWAP
│   │   │   ├── margin.go                #   保證金持倉量
│   │   │   ├── crossccy.go              #   跨幣種 Funding Rate
│   │   │   ├── intraday.go              #   日內時段 + 月內修正
│   │   │   └── regime.go                #   市場體制識別
│   │   │
│   │   ├── orderbook/                   # 掛單簿分析
│   │   │   ├── dust.go                  #   動態 Dust Filter
│   │   │   ├── wall.go                  #   巨單牆 + 分散牆偵測
│   │   │   ├── hidden.go                #   Hidden Ratio 估算
│   │   │   └── competitor.go            #   競爭者行為偵測
│   │   │
│   │   ├── strategy/                    # 策略決策（Phase 4）
│   │   │   ├── pricing.go               #   溢價係數 + MDC 映射
│   │   │   ├── floor.go                 #   利率地板 + 機會成本框架
│   │   │   ├── period.go                #   天數決策（曲線形態、線性縮放）
│   │   │   ├── lockup.go                #   鎖倉機會成本折扣
│   │   │   ├── allocation.go            #   資金分層佈署
│   │   │   ├── splitting.go             #   深度感知掛單拆分
│   │   │   ├── impact.go                #   自身市場衝擊管理
│   │   │   ├── queue.go                 #   排隊位置估算
│   │   │   ├── partial.go               #   部分成交管理
│   │   │   ├── stagger.go               #   到期時間分散
│   │   │   ├── weekend.go               #   週末遞減溢價
│   │   │   ├── calendar.go              #   特殊事件日曆
│   │   │   └── noise.go                 #   隨機擾動 + 心理價位避讓
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
│   │   └── quota/                       # 全局 API 配額分配器
│   │       └── allocator.go
│   │
│   │   # ── Domain 層 ──
│   │
│   ├── domain/
│   │   ├── user.go                      # User, UserStatus
│   │   ├── apikey.go                    # APIKey, KeyPermissions
│   │   ├── offer.go                     # FundingOffer, OfferParams
│   │   ├── credit.go                    # FundingCredit, CreditStatus
│   │   ├── execution.go                 # ExecutionRecord（V10 §9.1 全欄位）
│   │   ├── billing.go                   # BillingRecord, BillingPeriod
│   │   ├── snapshot.go                  # MarketSnapshot, RawMarketData
│   │   ├── signal.go                    # SignalValue, MDCResult, SignalFreshness
│   │   ├── regime.go                    # RegimeType, RegimeParams
│   │   └── config.go                    # StrategyConfig（附錄 B 全參數型別定義）
│   │
│   │   # ── Infrastructure 層 ──
│   │
│   ├── repository/
│   │   ├── interfaces.go                # 所有 Repository interface 集中定義
│   │   └── postgres/
│   │       ├── query/                   #   手寫 SQL 查詢（sqlc 輸入）
│   │       │   ├── user.sql
│   │       │   ├── apikey.sql
│   │       │   ├── execution.sql
│   │       │   ├── config.sql
│   │       │   └── billing.sql
│   │       ├── sqlc/                    #   sqlc 自動產生（勿手動修改）
│   │       │   ├── db.go                #     DBTX interface
│   │       │   ├── models.go            #     DB row struct
│   │       │   ├── querier.go           #     Querier interface
│   │       │   ├── user.sql.go          #     user query 實作
│   │       │   ├── apikey.sql.go
│   │       │   ├── execution.sql.go
│   │       │   ├── config.sql.go
│   │       │   └── billing.sql.go
│   │       ├── user.go                  #   手寫：包裝 sqlc → 實作 Repository interface
│   │       ├── apikey.go                #     sqlc model ↔ domain 型別轉換
│   │       ├── execution.go
│   │       ├── config.go
│   │       └── billing.go
│   │
│   ├── cache/
│   │   └── redis.go                     # Session、快取、Pub/Sub
│   │
│   ├── bitfinex/
│   │   ├── client.go                    # REST API 封裝
│   │   ├── ws.go                        # WebSocket 連接管理（公開 + 認證）
│   │   └── types.go                     # API 回應型別轉換
│   │
│   ├── notification/
│   │   ├── service.go                   # 統一通知入口
│   │   └── email.go                     # Email 發送實作
│   │
│   ├── crypto/
│   │   └── aes.go                       # AES-256-GCM 加解密
│   │
│   └── appconfig/
│       └── appconfig.go                 # 環境變數 / YAML 載入
│
├── schema/                                    # Atlas 宣告式 Schema（Desired State）
│   ├── schema.hcl                             #   完整資料庫 schema 定義
│   └── atlas.hcl                              #   Atlas 專案配置（env、data source）
│
├── migrations/                                # Atlas 版本化 Migration（自動產生）
│   ├── 20260307000001_create_users.sql
│   ├── 20260307000002_create_api_keys.sql
│   ├── 20260307000003_create_user_configs.sql
│   ├── 20260307000004_create_executions.sql
│   ├── 20260307000005_create_billing.sql
│   ├── 20260307000006_create_audit_log.sql
│   ├── 20260307000007_create_worker_snapshots.sql
│   └── atlas.sum                              #   Migration 完整性校驗
│
├── sqlc.yaml                                  # sqlc 配置
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── go.mod
└── go.sum
```

---

## 4. 各層職責與規範

### 4.1 Transport 層 (`api/`)

**職責**：HTTP 請求的進出口。只做參數解析、驗證、回應格式化。不包含業務邏輯。

**規範**：
- handler 只呼叫 `service/` 或讀取 `lending/` 的狀態，不直接操作 `repository/`。
- 每個 handler 方法不超過 30 行，邏輯複雜時委派給 service。
- 錯誤回應使用統一格式：`{ "error": { "code": "...", "message": "..." } }`。

**呼叫方向**：
```
handler/auth.go      → service/user.go
handler/apikey.go    → service/apikey.go
handler/config.go    → service/config.go → 通知 lending/worker 熱載入
handler/dashboard.go → 讀取 lending/ 的即時狀態（透過 channel / Redis）
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
    StartWorker(userID string, cfg domain.StrategyConfig) error
    StopWorker(userID string) error
}

type APIKeyService struct {
    repo    repository.APIKeyRepository
    crypto  *crypto.AES
    bfx     *bitfinex.Client
    workers WorkerManager   // fx 自動注入 *lending.Service
}

func NewAPIKeyService(repo repository.APIKeyRepository, crypto *crypto.AES,
    bfx *bitfinex.Client, workers WorkerManager) *APIKeyService { ... }
```

```go
// service/config.go — 同樣的模式
package service

// ConfigReloader 通知 Worker 熱載入新參數（由 lending.Service 隱式實作）
type ConfigReloader interface {
    ReloadConfig(userID string, cfg domain.StrategyConfig) error
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

type Service struct {
    marketFeed *marketfeed.Service
    workerPool *worker.Pool
    quota      *quota.Allocator
}

func NewService(deps Dependencies) *Service { ... }

// Start 啟動共享層 + Worker Pool，阻塞直到 ctx 取消
func (s *Service) Start(ctx context.Context) error { ... }

// Stop 優雅關閉：停止心跳 → 撤銷所有掛單 → 關閉 WS 連接
func (s *Service) Stop(ctx context.Context) error { ... }

// StartWorker 新增一個用戶的 Worker（由 service/apikey.go 呼叫）
func (s *Service) StartWorker(userID string, cfg domain.StrategyConfig) error { ... }

// StopWorker 停止一個用戶的 Worker
func (s *Service) StopWorker(userID string) error { ... }

// ReloadConfig 通知 Worker 熱載入新參數（由 service/config.go 呼叫）
func (s *Service) ReloadConfig(userID string, cfg domain.StrategyConfig) error { ... }
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

type Executor interface {
    PlaceOffer(ctx context.Context, params domain.OfferParams) error
    CancelOffer(ctx context.Context, offerID int64) error
}
```

`signal/`、`strategy/`、`execution/` 只依賴 `domain/`，自然滿足上述 interface（Go 的隱式實作），無需 import 消費方 package。由 `lending/service.go` 負責在啟動時將具體實作注入各消費方。

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
- `worker/` 依賴 `strategy/` 和 `execution/` 做決策與執行，自行定義 `Strategy`、`Executor` interface。
- **禁止循環依賴**：子 package 絕不 import parent `lending/`，子 package 之間不互相 import。所有組裝由 `lending/service.go` 完成。

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
    MDC          MDCResult
    Regime       RegimeType
    RegimeParams RegimeParams
    Signals      []SignalValue
    OrderBook    OrderBookSummary
    WallPositions []WallPosition
    HiddenRatio  float64
    FlashFreeze  bool
    Timestamp    time.Time
}

// domain/config.go — 對應 V10 附錄 B 全部參數
type StrategyConfig struct {
    Signal    SignalConfig    `json:"signal"`
    Execution ExecutionConfig `json:"execution"`
    Period    PeriodConfig    `json:"period"`
    Safety    SafetyConfig    `json:"safety"`
    // ...附錄 B 的所有參數群組
}
```

### 4.5 Infrastructure 層

#### `repository/`

**規範**：
- `interfaces.go` 集中定義所有 Repository interface，放在 `repository` package 頂層。
- `postgres/query/` 放手寫的 SQL 查詢檔（sqlc 輸入）。
- `postgres/sqlc/` 放 sqlc 自動產生的 Go 檔案（**勿手動修改**，由 `sqlc generate` 產生）。
- `postgres/*.go` 是手寫的薄包裝層，負責：
  - 持有 `sqlc.Queries` 實例
  - 實作 `repository.XxxRepository` interface
  - 將 `sqlc/models.go` 的 DB row struct 轉換為 `domain/` 的業務型別
- 未來如需更換資料庫，新增子目錄（如 `mysql/`）即可，不影響上層。

```go
// repository/interfaces.go — 手寫，定義業務契約（使用 domain 型別）
package repository

type UserRepository interface {
    Create(ctx context.Context, user *domain.User) error
    GetByID(ctx context.Context, id string) (*domain.User, error)
    GetByEmail(ctx context.Context, email string) (*domain.User, error)
    UpdateStatus(ctx context.Context, id string, status domain.UserStatus) error
}

type ExecutionRepository interface {
    Save(ctx context.Context, record *domain.ExecutionRecord) error
    QueryByUser(ctx context.Context, userID string, since time.Time) ([]domain.ExecutionRecord, error)
    AggregateWeeklyAlpha(ctx context.Context, userID string, since time.Time) (*domain.AlphaReport, error)
}

// ... 其他 Repository interface
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

**職責**：對 `bitfinex-api-go/v2` 的薄包裝，隔離第三方 SDK 的 API 變化。

**規範**：
- 上層不直接 import `bitfinex-api-go`，只透過這個 package 存取。
- `client.go` 封裝 REST API（查詢餘額、驗證權限、提交掛單）。
- `ws.go` 管理 WebSocket 連接（公開頻道 + 每用戶認證頻道），含自動重連邏輯。
- `types.go` 將 SDK 的型別轉換為 `domain/` 的型別。

#### `cache/`

**職責**：Redis 操作封裝。

**使用場景**：
- Session 儲存（JWT refresh token）。
- `MarketSnapshot` 快取（Dashboard 讀取用，避免直接讀 lending 內部狀態）。
- 未來多機部署時作為 MarketSnapshot 廣播的 Pub/Sub 通道。

---

## 5. 兩條呼叫鏈

### 5.1 CRUD 請求（HTTP 驅動）

以「用戶修改策略參數」為例：

```
[Frontend] POST /api/v1/configs
    │
    ▼
router.go → middleware/jwt.go → handler/config.go
    │         解析 + 驗證 request body
    ▼
service/config.go
    │  1. 呼叫 domain.StrategyConfig.Validate() 驗證參數合理性
    │  2. 呼叫 repository.ConfigRepository.Save() 持久化
    │  3. 呼叫 ConfigReloader.ReloadConfig(userID, newConfig) 通知 Worker
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
        // ── Infrastructure ──
        fx.Provide(appconfig.Load),
        fx.Provide(postgres.Connect),
        fx.Provide(cache.NewRedis),
        fx.Provide(bitfinex.NewClient),
        fx.Provide(crypto.NewAES),
        fx.Provide(notification.NewService),

        // ── Repository ──
        fx.Provide(
            sqlcpg.NewUserRepo,
            sqlcpg.NewAPIKeyRepo,
            sqlcpg.NewConfigRepo,
            sqlcpg.NewExecutionRepo,
            sqlcpg.NewBillingRepo,
        ),

        // ── Lending Engine ──
        // fx 自動將 *lending.Service 注入為 service.WorkerManager / service.ConfigReloader
        fx.Provide(lending.NewService),

        // ── Service 層 ──
        fx.Provide(
            service.NewUserService,
            service.NewAPIKeyService,     // 接收 WorkerManager interface
            service.NewConfigService,     // 接收 ConfigReloader interface
            service.NewBillingService,
        ),

        // ── Transport 層 ──
        fx.Provide(
            handler.NewAuthHandler,
            handler.NewUserHandler,
            handler.NewAPIKeyHandler,
            handler.NewConfigHandler,
            handler.NewBillingHandler,
            handler.NewDashboardHandler,
            handler.NewHealthHandler,
        ),
        fx.Provide(api.NewRouter),

        // ── 生命週期管理 ──
        fx.Invoke(registerHooks),
    ).Run()
}

// registerHooks 註冊 HTTP server 和 Lending Engine 的啟停
func registerHooks(lc fx.Lifecycle, cfg *appconfig.Config, router http.Handler, lendingSvc *lending.Service) {
    srv := &http.Server{Addr: cfg.ListenAddr, Handler: router}

    lc.Append(fx.Hook{
        OnStart: func(ctx context.Context) error {
            // 啟動放貸引擎
            go lendingSvc.Start(ctx)
            // 啟動 HTTP server
            go srv.ListenAndServe()
            return nil
        },
        OnStop: func(ctx context.Context) error {
            // fx 收到 SIGINT/SIGTERM 自動觸發 OnStop
            lendingSvc.Stop(ctx)
            return srv.Shutdown(ctx)
        },
    })
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
               api/handler   service/    lending/
                    │           │           │
                    │           │     ┌─────┼─────────────────┐
                    │           │     ▼     ▼     ▼     ▼     ▼
                    │           │  market  signal order  strat  worker
                    │           │  feed/   /     book/  egy/   /
                    │           │     │                  │     │
                    │           │     │   (純計算，只依賴 domain/)
                    │           │     │                  │     │
                    ├───────────┤     │                  │     │
                    │           │     │                  │     │
                    ▼           ▼     ▼                  ▼     ▼
              ┌─────────────────────────────────────────────────┐
              │  repository/    bitfinex/    cache/             │
              │  (postgres)                                     │
              └────────────────────┬────────────────────────────┘
                                   │
                                   ▼
                              domain/
                         （零外部依賴）
```

**箭頭方向 = import 方向。所有層最終依賴 `domain/`，`domain/` 不依賴任何人。**
**Infrastructure 層（repository/, bitfinex/, cache/）依賴 `domain/` 做型別轉換，但不被 `domain/` 反向依賴。**

**禁止的依賴**：
- `domain/` → 任何其他 package ❌
- `repository/` → `service/` 或 `lending/` ❌（上層依賴下層，不可反向）
- `signal/` → `bitfinex/` ❌（信號計算是純邏輯，不做 I/O）
- `strategy/` → `bitfinex/` ❌（同上）
- `service/` → `lending/` ❌（`service/` 定義自己的小 interface 如 `WorkerManager`，fx 自動注入 `*lending.Service`，無需 import）

---

## 8. Package 命名對照表

| Package | import path | 用途 | 對應 V10 章節 |
| :--- | :--- | :--- | :--- |
| `handler` | `internal/api/handler` | HTTP 請求處理 | §10.8 |
| `middleware` | `internal/api/middleware` | JWT, Rate Limit | §10.8 |
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
| `repository` | `internal/repository` | 資料存取介面 | §10.10 |
| `postgres` | `internal/repository/postgres` | Repository interface 實作（手寫包裝層） | §10.10 |
| `sqlc` | `internal/repository/postgres/sqlc` | sqlc 自動產生的 query 程式碼（勿手改） | §10.10 |
| `cache` | `internal/cache` | Redis | §10.2 (Pub/Sub) |
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

### 9.7 為什麼 Repository interface 放在 `repository/` 而不是 `domain/`

Go 慣例是在「使用方」定義 interface（consumer-side interface）。但 Repository interface 被多個使用方（`service/`、`lending/execution/`）共用，放在 `domain/` 會讓 domain 對 `context`、`time` 以外的標準庫產生隱式依賴。放在 `repository/` package 頂層是務實的折衷——它是 interface 的「家」，`postgres/` 子目錄是實作。

---

## 10. 擴展路徑

### 10.1 單機 → 多機（> 500 用戶）

- `lending/marketfeed/` 的 MarketSnapshot 廣播從 Go channel 切換到 Redis Pub/Sub。
- Strategy Engine 可以部署多台，每台承載一批 Worker。
- `quota/allocator.go` 改為從 Redis 讀取全局配額狀態（分散式配額）。
- `repository/` 和 `cache/` 不需要改動（已經是外部服務）。

### 10.2 單體 → 微服務（> 2,000 用戶）

拆分邊界已經由 package 結構定義好：

| 微服務 | 包含的 package | 說明 |
| :--- | :--- | :--- |
| API Service | `api/`, `service/`, `domain/`, `repository/` | HTTP + CRUD |
| Lending Engine | `lending/`, `domain/`, `repository/`, `bitfinex/` | 策略 + 執行 |
| Billing Service | `billing/`, `domain/`, `repository/` | 計費 |
| Notification Service | `notification/` | 告警 |

package 間目前透過 Go interface 呼叫的地方，改為 gRPC 即可。`domain/` 的 struct 直接映射為 protobuf message。
