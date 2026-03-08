## Why

D1-D4 處理單筆掛單的利率和天數，但都假設一次性出借全部可用資金。實際上，將資金分成多層（tranches）部署在不同利率/天數組合上，可以降低集中風險並捕捉更多成交機會。Allocation Strategy 決定如何將可用資金分層分配。

## What Changes

- 新增 `lending/strategy/allocation.go`：Allocation Strategy 模組
  - 資金分層：根據可用餘額大小決定分幾層（小額不拆、大額 2-3 層）
  - 分層比例：核心層（保守利率、大部分資金）+ 探索層（較高利率、少量資金）
  - Regime 調整：crisis 時集中為單層（不分散）、contango 時更積極分層
  - 輸出：多筆 OfferDecision，各自有不同的 amount 分配
- 新增完整單元測試

## Capabilities

### New Capabilities
- `allocation-strategy`: 資金分層部署策略，將可用資金分配到不同利率/天數組合以分散風險

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/allocation.go`, `lending/strategy/allocation_test.go`
- 純計算模組，只依賴 `domain/`
