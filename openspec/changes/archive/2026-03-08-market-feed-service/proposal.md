## Why

B1–B3 已完成 WebSocket Client、Domain Types、Redis Cache 基礎設施。現在需要一個共享的 Market Feed Service 作為市場分析層的核心，負責：接收 WebSocket 即時數據 → 組裝 RawMarketData → 呼叫信號模組計算 → 組裝 MarketSnapshot → 快取 + 廣播。這是整個進階放貸引擎的「大腦」入口。

C1 專注於 Service 框架 + snapshot 組裝邏輯。信號計算模組（C3）、Order Book 分析（C4）、Regime Detection（C5）將在後續 change 實作，C1 先用 stub/placeholder 保持可執行。Flash Crash Detection（C2）會一同在此 change 實作，因為它是 Phase 1.5 的核心安全機制。

## What Changes

- 新增 `lending/marketfeed/service.go`：Market Feed Service 主循環，接收 WS 數據 → 組裝快照 → 廣播
- 新增 `lending/marketfeed/snapshot.go`：RawMarketData 收集 + MarketSnapshot 組裝（OrderBookSummary、WallPosition 偵測）
- 新增 `lending/marketfeed/flashcrash.go`：閃崩偵測器（利率急跌 / 成交量暴增）
- 新增 `lending/marketfeed/interfaces.go`：定義 SignalSource interface（consumer-side pattern）

## Capabilities

### New Capabilities

- `market-feed-core`: Market Feed Service 生命週期（Start/Stop）+ WebSocket 數據接收 + snapshot 廣播
- `market-feed-snapshot`: RawMarketData 收集、OrderBookSummary 計算、WallPosition 偵測、MarketSnapshot 組裝
- `flash-crash-detection`: 閃崩偵測器（利率異常波動 + 凍結機制）

### Modified Capabilities

（無）

## Impact

- **新增檔案**: `lending/marketfeed/service.go`, `snapshot.go`, `flashcrash.go`, `interfaces.go`
- **新增測試**: `lending/marketfeed/*_test.go`
- **依賴**: `bitfinex.WSClient`（B1）、`repository.SnapshotCache`（B3）、`domain/*`（B2）
- **後續影響**: C3 信號模組將實作 `SignalSource` interface 注入 Market Feed
