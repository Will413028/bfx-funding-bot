## Why

使用者掛出 funding offer 後，需要等待被借方（taker）成交。排隊位置越後面，等待時間越長，甚至可能永遠成交不了。Queue Position Strategy 估算自身 offer 在 order book 中的排隊位置，並據此調整利率（願意降低以加速成交）或建議取消重掛（當位置太差時）。

## What Changes

- 新增 `lending/strategy/queue.go`：Queue Position 模組
  - 排隊深度估算：計算自身 offer rate 之前有多少資金排隊
  - 成交時間估計：根據排隊深度和歷史成交速率估算等待時間
  - 利率折讓：排隊位置差時降低利率以提高成交機率
  - 取消建議：排隊時間過長的 offer 建議取消重掛
- 新增完整單元測試

## Capabilities

### New Capabilities
- `queue-strategy`: 評估 offer 在 order book 中的排隊位置，調整利率或建議取消以提高成交效率

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/queue.go`, `lending/strategy/queue_test.go`
- 純計算模組，只依賴 `domain/`
