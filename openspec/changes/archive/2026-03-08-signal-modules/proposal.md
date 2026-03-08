## Why

Market Feed Service（C1）已定義 `SignalSource` interface 並預留信號計算插入點。需要實作 6 個信號模組 + MDC 聚合器，為策略決策層提供量化市場方向信號。

## What Changes

- 新增 `lending/signal/mdc.go`：MDC 聚合器（freshness decay + recovery smoothing + tanh 壓縮）
- 新增 `lending/signal/book.go`：Order Book 消耗速率信號
- 新增 `lending/signal/liquidation.go`：清算瀑布偵測（hard override + 3-step 退場）
- 新增 `lending/signal/momentum.go`：雙速 VWAP 動量信號
- 新增 `lending/signal/margin.go`：保證金多空倉位變化信號
- 新增 `lending/signal/crossccy.go`：跨幣種 funding rate 相關性信號

## Capabilities

### New Capabilities

- `signal-mdc-aggregator`: MDC 加權聚合（freshness decay、recovery smoothing、liquidation hard override）
- `signal-computation`: 5 個獨立信號計算模組（book、liquidation、momentum、margin、crossccy）

### Modified Capabilities

（無）

## Impact

- **新增檔案**: `lending/signal/` 目錄下 6 個 Go 檔案 + 測試
- **無修改**: 純新增，透過 `SignalSource` interface 注入 Market Feed
- **依賴**: 僅依賴 `domain/` 層型別
