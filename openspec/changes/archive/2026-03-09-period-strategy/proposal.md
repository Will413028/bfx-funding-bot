## Why

D1 Pricing 和 D2 Floor 處理利率決策，但天數（period）目前固定用 `config.Period.Min`。Period Strategy 根據市場體制和利率水準動態選擇最佳出借天數——contango 時延長天數鎖定高利率，backwardation 時縮短天數保持靈活性，crisis 時用最短天數。這直接影響收益和資金流動性的平衡。

## What Changes

- 新增 `lending/strategy/period.go`：Period Strategy 模組
  - Regime 驅動基礎天數：contango 偏長、backwardation 偏短、crisis 最短、neutral 中間值
  - 利率-天數線性縮放：利率越高（相對 FRR），越值得鎖定更長天數
  - 波動率縮短：高波動時縮短天數以保持靈活性
  - 結果 clamp 到 config.Period.Min ~ config.Period.Max
- 新增完整單元測試

## Capabilities

### New Capabilities
- `period-strategy`: 基於市場體制、利率水準和波動率動態決定出借天數的策略模組

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/period.go`, `lending/strategy/period_test.go`
- 純計算模組，只依賴 `domain/`
