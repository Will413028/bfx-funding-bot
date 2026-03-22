# Backend — Go (Gin + fx + zap)

## 快速指令

```bash
make dev              # 啟動 dev server（FRONTEND_URL=http://localhost:3000）
make build            # 編譯到 ./bin/server
make sqlc             # 產生 sqlc 程式碼
make migrate-diff name # 產生 migration（需要 Docker）
make migrate-apply    # 套用 migration 到 Neon
```

## 目錄結構

```
cmd/server/main.go          # 入口，fx DI 組裝
internal/
  appconfig/                # 環境變數 → Config struct
  auth/                     # JWT RSA256 簽發/驗證
  bitfinex/                 # Bitfinex REST client
  crypto/                   # AES-256 加密（API secret 儲存）
  domain/                   # Domain models + AppError 定義
  handler/                  # HTTP handlers（Gin）+ router 註冊
  infra/                    # DB pool / Redis 連線
  lending/                  # Lending engine（worker pool, market feed, signals）
  middleware/               # JWT auth, rate limiting, request ID, CORS, logger, recovery
  notification/             # Email（Resend）
  repository/               # Data access（Postgres sqlc + Redis）
  service/                  # Business logic
schema/                     # Atlas HCL schema 定義
migrations/                 # 自動產生的 SQL migration 檔案
```

## 架構慣例

### 三層架構 + Consumer-Side Interface

```
Handler → Service → Repository
```

- Service 定義自己需要的 interface，Repository 實作
- fx.Annotate + fx.As 綁定 interface（見 `repository/di.go`）
- 不可跨層呼叫（Handler 不直接碰 Repository）

### 錯誤處理

統一使用 `domain.AppError`：

```go
type AppError struct {
    StatusCode int
    Code       string  // e.g. "INVALID_EMAIL"
    Message    string
}
```

- Handler 用 `errors.As(err, &appErr)` 判斷後回傳對應 HTTP status
- 非 AppError → 500 INTERNAL_ERROR

### API 回應格式

```json
// 成功
{ "data": { ... } }

// 錯誤
{ "error": { "code": "ERROR_CODE", "message": "..." } }

// 分頁（keyset pagination，cursor = base64(unixMillis:id)）
{ "data": [...], "pagination": { "nextCursor": "...", "hasMore": false } }
```

### 安全機制

- JWT: RSA256，支援 PKCS8 + PKCS1 格式，24hr 過期
- Password: bcrypt cost=12，8–72 字元
- API Secret: AES-256 加密存 DB，回傳時 mask 為 "****"
- Rate Limit: per-IP token bucket（auth 5r/s, WS 2r/s, protected 20r/s）
- WebSocket: 短效 token 認證

## 測試

- 每層用 in-memory mock repo 測試，不依賴 DB
- `assertAppErrorCode(t, err, "CODE")` 共用 helper（定義在 `service/user_test.go`）
- Handler test 用 `httptest.NewRecorder` + `gin.TestMode`
- 執行：`go test ./...`

## sqlc 工作流

1. 修改 `internal/repository/postgres/query/*.sql`
2. 執行 `make sqlc`（或 `sqlc generate`）
3. `internal/repository/postgres/sqlc/` 是自動產生的，**不可手改**

## Atlas Migration 工作流

1. 修改 `schema/schema.hcl`
2. `cd schema && atlas migrate diff <name> --env neon`（需要 Docker）
3. 檢查 `migrations/` 產生的 SQL
4. `cd schema && source ../../.env && atlas migrate apply --env neon`
5. **不可用 MCP 直接對 DB 執行 SQL 來代替 migration**

## 背景服務

- **Lending Service**: 每用戶 worker pool，quota 管理
- **Market Feed**: WebSocket 連 Bitfinex，廣播 snapshot
- **WebSocket Hub**: 管理 client 連線，廣播 engine 事件

## 主要依賴

| 套件 | 用途 |
|------|------|
| gin v1.12 | HTTP framework |
| uber/fx v1.24 | DI container |
| uber/zap v1.27 | Structured logging |
| pgx/v5 | PostgreSQL driver |
| go-redis/v9 | Redis client |
| golang-jwt/v5 | JWT |
| gorilla/websocket | WebSocket |
| axiom-go | 雲端日誌（optional） |
| resend | Email 通知 |

## Docker

- Multi-stage build：golang:1.25-alpine → alpine:3.21
- CGO_ENABLED=0，binary: `./server`
- Port: 8080
