## Why

使用者存好 API Key 後，需要設定放貸策略參數（利率範圍、天數、金額等），放貸引擎才能根據這些參數自動操作。Strategy Config 是 API Key 與放貸引擎之間的橋樑，是 MVP 放貸功能的前置條件。

## What Changes

- 新增 `domain.StrategyConfig` 領域模型，包含 MVP 所需的放貸策略參數（利率、天數、金額範圍）及 `Validate()` 方法
- 新增 `user_configs` 資料表，使用 JSONB 儲存策略設定，每個使用者一筆設定
- 新增 ConfigRepository interface 及 PostgreSQL 實作（sqlc）
- 新增 ConfigService（CRUD 業務邏輯 + 驗證）
- 新增 ConfigHandler（PUT/GET/DELETE `/configs` endpoints）
- 新增 Atlas migration 和 sqlc 生成檔

## Capabilities

### New Capabilities
- `strategy-config-crud`: 策略設定 CRUD API endpoints（建立/讀取/更新/刪除）
- `strategy-config-validation`: 策略設定參數驗證規則（利率範圍、天數限制、金額上下限）

### Modified Capabilities
- `app-config`: 無需修改（不新增環境變數）

## Impact

- **新增 DB table**: `user_configs`（user_id UNIQUE + JSONB config 欄位）
- **新增 API endpoints**: PUT/GET/DELETE `/api/v1/configs`（JWT 保護）
- **修改 router.go**: 註冊新的 config routes
- **修改 main.go**: fx.Provide ConfigService + ConfigHandler
- **修改 repository/**: 新增 ConfigRepository interface + postgres 實作
