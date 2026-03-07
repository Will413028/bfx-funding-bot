## Context

後端目前只有空的 `cmd/server/main.go` 和 `internal/` 目錄結構。需要建立完整的 HTTP 基礎架構，供後續認證、API Key 管理、放貸引擎等業務模組使用。

部署平台為 Koyeb（Docker），前端部署在 Vercel，兩者不同 origin，需要 CORS 支援。Koyeb 透過 SIGTERM 通知應用關閉，需要 graceful shutdown。

## Goals / Non-Goals

**Goals:**
- 建立可擴展的 HTTP server + router + middleware 架構
- 統一錯誤處理與回應格式
- fx 依賴注入管理所有元件生命週期
- Graceful shutdown 支援 Koyeb 部署
- Health check 供 Koyeb readiness probe 使用
- 結構化日誌（zap）
- CORS + Request ID middleware

**Non-Goals:**
- JWT 認證（屬於後續 `user-auth` 變更）
- 資料庫連線（屬於後續變更）
- Redis 連線（屬於後續變更）
- 任何業務 API endpoint

## Decisions

### 1. HTTP 框架：Gin

**選擇**：Gin
**替代方案**：Echo、Chi、標準庫 net/http
**理由**：架構文件已決定使用 Gin/Echo。選 Gin 因為社區最大、middleware 生態完整、效能優秀。與 Echo 功能接近，但 Gin 的 GitHub star 和社區活躍度更高。

### 2. 依賴注入：fx

**選擇**：go.uber.org/fx
**替代方案**：手動 wire、Google Wire
**理由**：架構文件已決定。fx 的自動依賴解析和生命週期管理（OnStart/OnStop）天然適合管理 HTTP server + 未來的 Lending Engine 雙生命週期。

### 3. 日誌：zap

**選擇**：go.uber.org/zap
**替代方案**：slog（標準庫）、zerolog
**理由**：與 fx 同為 Uber 開源，原生整合（`fx.WithLogger`）。Gin 有成熟的 `gin-contrib/zap` middleware。零分配設計在放貸引擎高頻日誌場景下效能最佳。專案已依賴 Uber 生態（fx），不增加供應商風險。

### 4. 配置管理：環境變數直讀

**選擇**：直接讀取 `os.Getenv` + 啟動時驗證
**替代方案**：Viper、envconfig
**理由**：初期設定項少（PORT、FRONTEND_URL），不需要引入 Viper 的複雜度。用一個 `appconfig.Load()` 函式集中讀取和驗證，缺少必要變數時 fail fast。

### 5. Router 結構：版本化路由群組

```
/api/v1/health    → handler.Health
/api/v1/auth/*    → (未來)
/api/v1/api-keys  → (未來)
/api/v1/configs   → (未來)
```

所有 API 在 `/api/v1` 下，方便未來版本升級。

### 6. 錯誤處理：統一 AppError 型別

定義 `domain.AppError` 結構，包含 HTTP status code、error code、message。handler 層統一透過 `c.JSON(err.StatusCode, gin.H{"error": ...})` 回傳。Gin 的 `Recovery` middleware 捕捉 panic，回傳 500。

## Risks / Trade-offs

- **[Gin 與 fx 整合]** → fx 管理 Gin engine 的 start/stop，需確保 `http.Server.Shutdown()` 被正確呼叫。透過 `fx.Lifecycle` 的 `OnStop` hook 實現。
- **[CORS 設定硬編碼]** → 初期 `FRONTEND_URL` 從環境變數讀取，只允許單一 origin。未來多租戶自訂域名時需改為動態 CORS。目前足夠。
- **[無 rate limiting]** → 初期不實作 rate limiting，等認證系統完成後再加上 per-user rate limit。
