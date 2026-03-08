## Why

E2 CreditManager 逐一續約到期 credit，每筆 credit 產生一次 SubmitFundingOffer API 呼叫。當使用者有大量 credits 同天到期（例如 20 筆），逐一續約會消耗過多 API 配額且產生碎片化的小額 offer。Batch Expiry 將同天到期的 credits 合併，產生更少但金額更大的 offer，減少 API 呼叫並改善市場衝擊。

## What Changes

- 新增 `lending/execution/batch.go`：BatchProcessor 模組
  - `BatchProcessor` struct，持有 FundingClient、ExecutionLog
  - `BatchCredits(credits, config) []CreditBatch` — 按到期日分組 credits，合併金額
  - `CreditBatch` 包含：到期日、合併金額、加權平均 rate、建議 period、原始 credit IDs
  - 合併規則：同天到期的 AutoRenew credits 合併為一筆，rate 取加權平均（按金額），period 取最大值
  - 金額上限：單筆 batch 不超過 config.Amount.Max，超過時拆成多筆
- 新增完整單元測試

## Capabilities

### New Capabilities
- `batch-expiry`: 批次合併到期 credits，減少 API 呼叫

### Modified Capabilities

## Impact

- 新增檔案：`lending/execution/batch.go`, `lending/execution/batch_test.go`
- 複用 E1 定義的 consumer-side interfaces
- 供 E2 CreditManager 或 worker 層呼叫（純計算，不做 API 呼叫）
