## Why

用戶儲存 Bitfinex API Key 時，系統目前只做加密儲存，不驗證 key 是否有效。無效的 key 會在後續放貸操作時才失敗，造成不好的用戶體驗。需要在儲存時即時驗證 key 的有效性，並回傳 funding wallet 資訊作為確認。

## What Changes

- 修改 `APIKeyService.Create` — 儲存前先呼叫 `bitfinex.Client.VerifyCredentials` 驗證 key 有效性
- 新增 `POST /api-keys/verify` endpoint — 獨立驗證端點，讓前端可以在儲存前先驗證
- 修改 Create API 回應 — 驗證成功時回傳 funding wallet balance 資訊
- 新增 `domain.APIKey.ExchangeStatus` 欄位 — 記錄 key 的驗證狀態（verified / unverified）
- 修改 DB schema 新增 `exchange_status` 欄位

## Capabilities

### New Capabilities
- `apikey-verification`: API Key 驗證流程（儲存時驗證、獨立驗證端點、狀態追蹤）

### Modified Capabilities
- `apikey-crud`: Create 流程加入驗證步驟，回應增加驗證狀態和餘額資訊

## Impact

- **修改 service**: `APIKeyService` 新增 `bitfinex.Client` 依賴
- **修改 handler**: `APIKeyHandler` 新增 Verify endpoint，Create 回應增加欄位
- **修改 domain**: `APIKey` struct 增加 `ExchangeStatus` 欄位
- **修改 DB schema**: `api_keys` 表新增 `exchange_status` 欄位 + Atlas migration
- **修改 DI**: `APIKeyService` constructor 需注入 `bitfinex.Client`
