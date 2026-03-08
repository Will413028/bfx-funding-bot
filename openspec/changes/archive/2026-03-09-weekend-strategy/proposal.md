## Why

週末市場流動性低，借款需求下降但 offer 供給不變，導致利率偏低。Weekend Premium Strategy 在週末掛單時加入遞減溢價，補償流動性風險。越接近週末開始溢價越高，週日晚間接近工作日時溢價遞減。

## What Changes

- 新增 `lending/strategy/weekend.go`：Weekend Premium 模組
  - 判斷當前是否為週末（週六/週日）
  - 週末時加入利率溢價，週六較高，週日遞減
  - 週五晚間也加入小幅溢價（預期隔夜進入週末）
  - 工作日不調整
- 新增完整單元測試

## Capabilities

### New Capabilities
- `weekend-strategy`: 週末遞減利率溢價，補償低流動性風險

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/weekend.go`, `lending/strategy/weekend_test.go`
- 純計算模組，只依賴 `domain/`
