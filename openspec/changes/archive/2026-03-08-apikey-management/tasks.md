## 1. Database Schema & Migration

- [x] 1.1 擴充 `schema/schema.hcl`：新增 `api_keys` table（id, user_id UNIQUE FK, label, api_key, api_secret BYTEA, created_at, updated_at）
- [x] 1.2 執行 `atlas migrate diff` 產生 migration 檔案
- [x] 1.3 執行 `atlas migrate apply` 部署到 Neon

## 2. Config & Crypto

- [x] 2.1 擴充 `internal/appconfig/config.go`：新增 `AESKey []byte` 欄位，從 `AES_KEY` 環境變數載入 hex 並 decode
- [x] 2.2 建立 `internal/crypto/aes.go`：`AES` struct，提供 `NewAES(key []byte) (*AES, error)`、`Encrypt(plaintext []byte) ([]byte, error)`、`Decrypt(ciphertext []byte) ([]byte, error)`
- [x] 2.3 更新 `.env.example`：新增 `AES_KEY` placeholder 及產生說明

## 3. Domain Layer

- [x] 3.1 建立 `internal/domain/apikey.go`：`APIKey` struct（ID, UserID, Label, APIKey, APISecret masked, CreatedAt, UpdatedAt）
- [x] 3.2 擴充 `internal/domain/errors.go`：新增 `ErrAPIKeyAlreadyExists`、`ErrAPIKeyNotFound`

## 4. Repository Layer

- [x] 4.1 建立 `internal/repository/postgres/query/apikey.sql`：CreateAPIKey、GetAPIKeyByID、GetAPIKeyByUserID、DeleteAPIKey 的 SQL 查詢
- [x] 4.2 執行 `sqlc generate` 產生 Go 程式碼
- [x] 4.3 擴充 `internal/repository/interfaces.go`：新增 `APIKeyRepository` interface
- [x] 4.4 建立 `internal/repository/postgres/apikey.go`：實作 `APIKeyRepository`，包含 sqlc model ↔ domain 型別轉換
- [x] 4.5 更新 `internal/repository/postgres/di.go` 和 `internal/repository/di.go`：fx 註冊 APIKeyRepo

## 5. Service Layer

- [x] 5.1 建立 `internal/service/apikey.go`：`APIKeyService` struct，注入 `APIKeyRepository` + `crypto.AES`
- [x] 5.2 實作 `Create(ctx, userID, apiKey, apiSecret, label) (*domain.APIKey, error)`：加密 secret、呼叫 repo、處理 duplicate
- [x] 5.3 實作 `List(ctx, userID) ([]domain.APIKey, error)` 和 `GetByID(ctx, userID, keyID) (*domain.APIKey, error)`
- [x] 5.4 實作 `Delete(ctx, userID, keyID) error`：ownership 檢查 + 刪除

## 6. Transport Layer

- [x] 6.1 建立 `internal/handler/apikey.go`：`APIKeyHandler` struct，注入 `APIKeyService`
- [x] 6.2 實作 Create、List、GetByID、Delete handler 方法
- [x] 6.3 修改 `internal/handler/router.go`：在 JWT protected group 下新增 apikeys 路由

## 7. DI Wiring

- [x] 7.1 修改 `cmd/server/main.go`：fx.Provide 新增 `crypto.NewAES`、`service.NewAPIKeyService`、`handler.NewAPIKeyHandler`

## 8. Testing

- [x] 8.1 為 `crypto.AES` 撰寫 unit test：roundtrip、random nonce、tamper detection、invalid key size
- [x] 8.2 為 `service.APIKeyService` 撰寫 unit test：create、duplicate、list、get、delete、ownership enforcement
- [x] 8.3 為 apikey handler 撰寫 unit test：各 endpoint 的 happy path 和 error cases
