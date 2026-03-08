## Why

Phase C 的市場分析層（Market Feed Service、閃崩偵測、信號模組）和 Phase D 的策略決策層都需要統一的市場數據型別。目前 `bitfinex/ws_types.go` 定義了 WebSocket 原始數據型別（基礎設施層），但 domain 層缺少聚合後的市場快照、信號值、市場體制等型別。需要先建立這些 domain types 作為後續模組的共同語言。

## What Changes

- 新增 `domain/snapshot.go`：MarketSnapshot（聚合市場快照）、RawMarketData（原始數據包裝）、OrderBookSummary、WallPosition
- 新增 `domain/signal.go`：SignalValue（信號計算結果）、MDCResult（Market Demand Curve 結果）、SignalType enum
- 新增 `domain/regime.go`：RegimeType enum（市場體制分類）、RegimeParams（體制參數）

## Capabilities

### New Capabilities

- `market-snapshot`: 市場快照聚合型別（MarketSnapshot、RawMarketData、OrderBookSummary、WallPosition）
- `market-signal`: 市場信號型別（SignalValue、MDCResult、SignalType）
- `market-regime`: 市場體制型別（RegimeType、RegimeParams）

### Modified Capabilities

（無）

## Impact

- **新增檔案**: `domain/snapshot.go`, `domain/signal.go`, `domain/regime.go`
- **無修改**: 純新增型別定義，不影響既有程式碼
- **後續影響**: Phase C (Market Feed Service) 和 Phase D (策略模組) 將使用這些型別
