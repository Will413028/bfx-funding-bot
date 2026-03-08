## Why

如果每次掛單都用精確計算的利率，其他市場參與者容易辨識出自動化策略的模式並加以利用。Noise Strategy 加入隨機擾動讓掛單利率略有變化，並避開心理價位（如 0.0005、0.001 等整數關卡），讓策略行為更難被預測。

## What Changes

- 新增 `lending/strategy/noise.go`：Noise 模組
  - 利率隨機擾動：在 ±1% 範圍內加入隨機偏移
  - 心理價位避讓：避開整數關卡（0.0005 的倍數），偏移到附近非整數位
  - 金額隨機擾動：±2% 範圍讓金額不完全相同
- 新增完整單元測試

## Capabilities

### New Capabilities
- `noise-strategy`: 隨機擾動利率和金額 + 心理價位避讓

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/noise.go`, `lending/strategy/noise_test.go`
- 純計算模組，只依賴 `domain/`
