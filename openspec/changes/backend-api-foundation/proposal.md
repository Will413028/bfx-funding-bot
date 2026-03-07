## Why

後端目前只有空的目錄結構和一個最小化的 `main.go`。需要建立 HTTP server、路由、middleware、health check、錯誤處理等基礎架構，作為後續所有業務功能（認證、API Key 管理、放貸引擎）的地基。沒有這層基礎，任何業務 API 都無法開發。

## What Changes

- 引入 Gin HTTP 框架，建立統一的 router 和 middleware chain
- 實作 health check endpoint（`GET /api/v1/health`）作為 Koyeb 的 readiness probe
- 建立統一錯誤回應格式 `{ "error": { "code": "...", "message": "..." } }`
- 引入 go.uber.org/fx 依賴注入框架，管理元件生命週期
- 實作 graceful shutdown（處理 SIGTERM，配合 Koyeb 部署）
- 建立應用配置模組（從環境變數讀取設定）
- 引入結構化日誌（zap）
- 建立 CORS middleware（允許 Vercel 前端跨域請求）
- 建立 request ID middleware（追蹤請求鏈路）

## Capabilities

### New Capabilities

- `http-server`: Gin HTTP server 啟動、路由註冊、middleware chain、graceful shutdown
- `app-config`: 環境變數讀取與驗證，集中管理所有設定項
- `error-handling`: 統一錯誤格式、panic recovery、錯誤碼定義
- `health-check`: Health / readiness endpoint，供 Koyeb 和前端偵測後端狀態

### Modified Capabilities

（無既有 capability）

## Impact

- **新增依賴**：`github.com/gin-gonic/gin`、`go.uber.org/fx`
- **影響程式碼**：`cmd/server/main.go`（改為 fx 啟動）、`internal/handler/`、`internal/middleware/`、`internal/appconfig/`
- **API**：新增 `GET /api/v1/health` endpoint
- **部署**：Koyeb health check path 需指向 `/api/v1/health`
