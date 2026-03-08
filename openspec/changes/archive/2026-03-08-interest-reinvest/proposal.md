## Why

放貸利息入帳後會停留在 funding wallet 的可用餘額中。如果不主動再投資，這些利息就是閒置資金，降低整體資金利用率。Interest Reinvest 偵測累積的利息收入，當金額達到可放貸門檻時自動提交新的 funding offer，實現複利效果。

## What Changes

- 新增 `lending/execution/interest.go`：InterestCollector 模組
  - `InterestCollector` struct，持有 FundingClient、KeyStore、Cipher、ExecutionLog
  - `CheckAndReinvest(ctx, userID, available, config)` — 判斷可用餘額中是否有足夠的閒置資金可再投資
  - 再投資門檻：available ≥ config.Amount.Min
  - 再投資金額：min(available, config.Amount.Max)
  - 使用 config.Rate.Min 和 config.Period.Min 作為保守的再投資參數
  - 記錄 ExecutionRecord (action="place", reason 帶 "reinvest" 標記)
- 新增完整單元測試

## Capabilities

### New Capabilities
- `interest-reinvest`: 閒置利息自動再投資

### Modified Capabilities

## Impact

- 新增檔案：`lending/execution/interest.go`, `lending/execution/interest_test.go`
- 複用 E1 定義的 consumer-side interfaces（FundingClient, KeyStore, Cipher, ExecutionLog）
