## Why

Phase C 已完成市場分析 pipeline（信號計算、MDC 聚合、體制偵測、掛單簿分析），產生了完整的 `MarketSnapshot`。現在需要建立策略決策層的第一個模組：Pricing Strategy，將市場狀態轉換為具體的掛單利率決策。這是 Phase D 的基礎——所有後續策略模組（floor、period、allocation 等）都依賴 pricing 產出的基準利率。

同時需要定義 `DecisionContext` 和 `DecisionResult` domain types，作為所有 13 個策略模組的共用介面型別。

## What Changes

- 新增 `domain/decision.go`：定義 `DecisionContext`（策略輸入）和 `DecisionResult`（策略輸出），包含掛單參數 `OfferDecision`
- 新增 `lending/strategy/pricing.go`：Pricing Strategy 模組，基於 MDC score 和市場體制計算溢價係數，產出建議利率
  - MDC → 溢價映射：MDC score 線性映射到溢價乘數
  - 體制調整：contango 加碼、backwardation 減碼、crisis 直接用地板利率
  - 掛單簿壓力調整：根據 bid/ask depth 比例微調
  - Wall awareness：偵測到 offer wall 時降低溢價
- 新增完整單元測試

## Capabilities

### New Capabilities
- `pricing-strategy`: 基於市場狀態（MDC、regime、order book）計算溢價係數和建議掛單利率的策略模組

### Modified Capabilities
- `lending-strategy`: 擴充策略框架，加入 `DecisionContext`/`DecisionResult` domain types

## Impact

- 新增檔案：`domain/decision.go`, `lending/strategy/pricing.go`, `lending/strategy/pricing_test.go`
- 修改 `lending-strategy` spec 以涵蓋新的 domain types
- 純計算模組，只依賴 `domain/`，不影響任何現有功能
- 為後續 D2-D13 策略模組建立基礎介面
