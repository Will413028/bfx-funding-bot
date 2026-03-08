## Why

加密貨幣市場在特定日期（如季度交割、重大升級、監管公告）借款需求會激增，利率飆漲。Event Calendar Strategy 內建已知的週期性事件日曆，在事件前後調整利率溢價以捕捉高利率機會，或在事件後利率回落前縮短天數避免鎖倉。

## What Changes

- 新增 `lending/strategy/calendar.go`：Event Calendar 模組
  - 內建週期性事件：每月月底結算、季度交割（3/6/9/12 月最後週五）
  - 事件前 3 天開始加溢價，事件當天最高，事件後 1 天遞減
  - 事件前建議縮短 period 避免跨事件鎖倉
- 新增完整單元測試

## Capabilities

### New Capabilities
- `calendar-strategy`: 基於事件日曆調整利率溢價與掛單天數

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/calendar.go`, `lending/strategy/calendar_test.go`
- 純計算模組，只依賴 `domain/`
