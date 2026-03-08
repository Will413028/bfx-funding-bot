## Why

D5 Allocation 將資金分成 1-3 層，但每層仍是一筆大單。大額掛單容易被市場偵測（形成 offer wall），影響成交價格且暴露策略意圖。Splitting Strategy 將單筆大額掛單拆分成多筆較小掛單，基於 order book 深度感知來決定拆分方式，降低市場衝擊。

## What Changes

- 新增 `lending/strategy/splitting.go`：Splitting Strategy 模組
  - 深度感知拆分：根據 order book ask depth 決定單筆最大金額（不超過深度的一定比例）
  - 均勻拆分：超過門檻的金額拆成 N 筆等額掛單
  - 最小金額保護：拆分後每筆 >= 50 USD
  - 輸出：拆分後的多筆 OfferDecision
- 新增完整單元測試

## Capabilities

### New Capabilities
- `splitting-strategy`: 深度感知的掛單拆分策略，將大額掛單拆成多筆以降低市場衝擊

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/splitting.go`, `lending/strategy/splitting_test.go`
- 純計算模組，只依賴 `domain/`
