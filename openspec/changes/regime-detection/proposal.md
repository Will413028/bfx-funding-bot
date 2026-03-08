## Why

`marketfeed/service.go` 的 Phase 3 目前是 placeholder（`regime := domain.RegimeNeutral`）。策略決策層 (Phase D) 需要知道當前市場處於哪種體制（contango / backwardation / neutral / crisis），才能選擇正確的定價策略和風險參數。體制識別是連結市場分析層和策略層的關鍵橋樑。

## What Changes

- 建立 `lending/signal/regime.go`：`RegimeDetector` struct，基於已計算的信號和 MDC 結果判定市場體制
- 輸出 `RegimeType` + `RegimeParams`（volatility、trend strength、demand/supply ratio、duration）
- 整合至 `marketfeed/service.go` 的 Phase 3，取代 placeholder

## Capabilities

### New Capabilities
- `regime-detection`: 基於 MDC score、信號值、ticker 數據的市場體制識別與參數輸出

### Modified Capabilities

(無)

## Impact

- 新增 `internal/lending/signal/regime.go` + 測試
- 修改 `internal/lending/marketfeed/service.go`：整合 `RegimeDetector`，取代 Phase 3 placeholder
