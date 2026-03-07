## 1. 依賴安裝與專案設定

- [x] 1.1 安裝 Gin (`github.com/gin-gonic/gin`)、fx (`go.uber.org/fx`)、zap (`go.uber.org/zap`) 依賴
- [x] 1.2 安裝 CORS middleware (`github.com/gin-contrib/cors`)

## 2. Domain 層 — 錯誤型別

- [x] 2.1 在 `internal/domain/errors.go` 定義 `AppError` struct（StatusCode, Code, Message）和常用錯誤建構函式（NewAppError, ErrNotFound, ErrInternal, ErrValidation）

## 3. App Config 模組

- [x] 3.1 在 `internal/appconfig/config.go` 定義 `Config` struct（Port, FrontendURL, Environment）
- [x] 3.2 實作 `Load()` 函式：從環境變數讀取、套用預設值、驗證必要欄位
- [x] 3.3 提供 fx module（`appconfig.Module`）匯出 `Config`

## 4. Middleware

- [x] 4.1 在 `internal/middleware/requestid.go` 實作 Request ID middleware（讀取或生成 UUID，設入 context 和 response header）
- [x] 4.2 在 `internal/middleware/logger.go` 實作 zap 請求日誌 middleware（自行實作，記錄 method, path, status, duration, request_id）
- [x] 4.3 在 `internal/middleware/recovery.go` 實作 panic recovery middleware（捕捉 panic，回傳統一錯誤格式）

## 5. Handler 層

- [x] 5.1 在 `internal/handler/health.go` 實作 `GET /api/v1/health` endpoint（回傳 `{ "status": "ok" }`）
- [x] 5.2 在 `internal/handler/router.go` 建立 router 設定：Gin engine、middleware chain、`/api/v1` 路由群組、404 handler、CORS 設定

## 6. 應用入口 — fx 整合

- [x] 6.1 改寫 `cmd/server/main.go`：使用 fx.New 組裝 appconfig、handler、middleware 模組
- [x] 6.2 實作 HTTP server 的 fx lifecycle hooks（OnStart: ListenAndServe, OnStop: Shutdown with 10s timeout）

## 7. 驗證

- [x] 7.1 手動測試：`go run ./cmd/server/`，確認 server 啟動並回應 health check
- [x] 7.2 手動測試：CORS preflight 請求正確回應
- [x] 7.3 手動測試：未知路由回傳 404 統一格式
- [x] 7.4 確認 `go build ./cmd/server/` 編譯成功
