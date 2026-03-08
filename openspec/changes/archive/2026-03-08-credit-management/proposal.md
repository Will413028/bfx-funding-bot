## Why

Offer 成交後變成 credit（債權），到期後資金釋出。如果使用者設定了 auto-renew，到期的 credit 應該自動以類似條件重新掛單。Credit Management 負責掃描即將到期的 credits，執行 auto-renew，並將到期未續約的 credit 標記為待重新分配。

## What Changes

- 新增 `lending/execution/credit.go`：Credit Management 模組
  - `CreditManager` struct，持有 FundingClient、KeyStore、Cipher、ExecutionLog
  - `ProcessCredits(ctx, userID, credits, config)` — 掃描 credits，對即將到期且 AutoRenew=true 的重新掛單
  - 到期判斷：剩餘天數 ≤ 1 視為即將到期
  - 續約使用原 rate 和 period（或 config 最小值取大者）
  - 記錄 ExecutionRecord (action="renew")
- 新增完整單元測試

## Capabilities

### New Capabilities
- `credit-management`: 自動續約即將到期的 credits

### Modified Capabilities

## Impact

- 新增檔案：`lending/execution/credit.go`, `lending/execution/credit_test.go`
- 複用 E1 定義的 consumer-side interfaces（FundingClient, KeyStore, Cipher, ExecutionLog）
