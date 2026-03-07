## Why

平台所有 API endpoint（API Key 管理、策略設定、計費查詢、Dashboard WebSocket）都需要識別用戶身份並驗證權限。目前後端只有 health check，缺乏認證系統。這是解鎖所有業務功能的前置條件。

## What Changes

- 新增用戶註冊 API（email + password），密碼以 bcrypt 雜湊儲存
- 新增用戶登入 API，驗證成功後簽發 JWT access token（RS256）
- 新增 JWT middleware，保護所有 `/api/v1/*` 路由（health 除外）
- 新增 `service/user.go` 作為認證業務邏輯層，編排 domain 驗證、密碼雜湊、JWT 簽發
- 新增 `internal/auth/jwt.go` 封裝 JWT 簽發與驗證邏輯（RS256 key pair）
- 擴充 `appconfig` 以載入 JWT key pair 相關環境變數

## Capabilities

### New Capabilities
- `user-auth`: 用戶註冊、登入、JWT 簽發與驗證的完整認證流程

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- **新增檔案**：`handler/auth.go`、`service/user.go`、`internal/auth/jwt.go`、`middleware/jwt.go`
- **修改檔案**：`handler/router.go`（註冊 auth 路由 + JWT middleware group）、`cmd/server/main.go`（fx.Provide 新增 service/handler）、`appconfig/config.go`（新增 JWT 相關 config 欄位）
- **新增依賴**：`golang-jwt/jwt/v5`、`golang.org/x/crypto`（bcrypt）
- **API 端點**：`POST /api/v1/auth/register`、`POST /api/v1/auth/login`
- **環境變數**：`JWT_PRIVATE_KEY`、`JWT_PUBLIC_KEY`（RS256 PEM）
