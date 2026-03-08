## Why

策略層（Phase D）輸出 `DecisionResult`（包含要掛的 offers、要取消的 offer IDs），但目前沒有模組負責將決策轉化為實際的 Bitfinex API 操作。Offer Execution 是執行層的核心，負責掛單、撤單、並將每個操作記錄到 execution_records 表。

## What Changes

- 新增 `lending/execution/offer.go`：Offer Execution 模組
  - `OfferExecutor` struct，持有 Bitfinex Client、API Key Repo、Execution Repo、AES cipher
  - `ExecuteDecision(ctx, userID, decision)` — 批量執行取消 + 掛單
  - 取消先於掛單（先釋放資金再重新部署）
  - 每個操作（成功或失敗）都記錄 ExecutionRecord
  - 部分失敗不中斷：取消失敗繼續、掛單失敗繼續，最後回報統計
- 新增完整單元測試（mock Bitfinex client + mock repos）

## Capabilities

### New Capabilities
- `offer-execution`: 將 DecisionResult 轉化為 Bitfinex API 操作並記錄

### Modified Capabilities

## Impact

- 新增檔案：`lending/execution/offer.go`, `lending/execution/offer_test.go`
- 依賴：`bitfinex/`, `repository/`, `crypto/`, `domain/`
- consumer-side interface pattern：execution 定義自己的依賴 interface
