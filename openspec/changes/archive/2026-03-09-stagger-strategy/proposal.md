## Why

如果所有債權（credits）在同一天到期，大量資金同時釋出重新掛單會造成市場衝擊，且錯過中間的利率機會。Stagger Strategy 評估現有 credits 的到期分佈，調整新掛單的天數以分散到期時間，確保資金持續滾動。

## What Changes

- 新增 `lending/strategy/stagger.go`：Stagger 模組
  - 分析 active credits 的到期日分佈，找出到期集中的日期
  - 調整新掛單 period 避開集中到期日
  - 到期過度集中時延長或縮短 period 以平衡分佈
- 新增完整單元測試

## Capabilities

### New Capabilities
- `stagger-strategy`: 分析到期分佈，調整掛單天數以分散到期時間

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/stagger.go`, `lending/strategy/stagger_test.go`
- 純計算模組，只依賴 `domain/`
