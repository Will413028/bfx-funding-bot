## Why

用戶需要將 Bitfinex API Key 綁定到平台，放貸引擎才能代替用戶操作掛單。API Key 包含 secret 和 key，屬於高敏感憑證，必須以 AES-256-GCM 加密儲存，且需在寫入前驗證 key 在 Bitfinex 的有效性與權限。這是啟動放貸引擎的前置條件。

## What Changes

- 新增 `api_keys` table（Atlas schema + migration），儲存加密後的 API Key
- 新增 AES-256-GCM 加解密模組 (`internal/crypto/aes.go`)
- 新增 API Key CRUD API：建立、查詢（列表/單筆，不回傳明文 secret）、刪除
- 新增 `APIKeyRepository` interface 及 PostgreSQL 實作
- 新增 `APIKeyService` 編排加密、解密、持久化邏輯
- 新增 `APIKeyHandler` 處理 HTTP 請求
- 擴充 `appconfig` 載入 `AES_KEY` 環境變數

## Capabilities

### New Capabilities
- `apikey-crud`: API Key 的建立、查詢、刪除 API 及加密儲存
- `apikey-encryption`: AES-256-GCM 加解密模組，保護 API Key secret

### Modified Capabilities
- `app-config`: 新增 `AES_KEY` 環境變數欄位（256-bit key for AES-256-GCM）

## Impact

- **新增檔案**：`internal/crypto/aes.go`、`internal/service/apikey.go`、`internal/handler/apikey.go`、`internal/repository/postgres/apikey.go`、`internal/repository/postgres/query/apikey.sql`、`internal/domain/apikey.go`
- **修改檔案**：`schema/schema.hcl`（新增 api_keys table）、`internal/repository/interfaces.go`（新增 APIKeyRepository）、`internal/repository/postgres/di.go`、`internal/repository/di.go`、`internal/handler/router.go`（新增 protected routes）、`cmd/server/main.go`（fx wiring）、`internal/appconfig/config.go`（AES_KEY）
- **新增依賴**：無（Go 標準庫 `crypto/aes`、`crypto/cipher` 即可）
- **DB Migration**：新增 `api_keys` table
- **API 端點**：`POST /api/v1/apikeys`、`GET /api/v1/apikeys`、`GET /api/v1/apikeys/:id`、`DELETE /api/v1/apikeys/:id`（全部需 JWT 認證）
- **環境變數**：`AES_KEY`（hex encoded 256-bit key）
