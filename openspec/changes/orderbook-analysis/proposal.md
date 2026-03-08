## Why

Market Feed Service (C1) 目前只有基礎的 `computeOrderBookSummary` 和 `detectWallPositions`，嵌在 `marketfeed/snapshot.go` 中。策略決策層 (Phase D) 需要更精密的掛單簿分析：過濾 dust 掛單雜訊、識別分散式巨單牆、估算隱藏掛單比例、偵測競爭者自動放貸行為。這些分析應獨立為 `lending/orderbook/` 純計算 package，遵守架構文件規範。

## What Changes

- 建立 `lending/orderbook/` package，4 個分析模組：
  - **Dust Filter**：動態閾值過濾小額 dust 掛單，降低掛單簿雜訊
  - **Wall Detection**：偵測單一巨單牆 + 多筆小單聚集的分散牆
  - **Hidden Ratio**：從成交量 vs 可見掛單簿深度差異估算隱藏掛單比例
  - **Competitor Detection**：識別自動放貸競爭者的掛單模式（跟價、round-number 掛單）
- 將 `marketfeed/snapshot.go` 中現有的 `computeOrderBookSummary` 和 `detectWallPositions` 遷移至 `orderbook/` package
- 更新 `marketfeed/service.go` 的 `assembleSnapshot` 使用新的 `orderbook/` 分析結果

## Capabilities

### New Capabilities
- `orderbook-dust-filter`: 動態 dust 掛單過濾（基於成交量百分位數的自適應閾值）
- `orderbook-wall-detection`: 巨單牆偵測（單一巨單 + 分散牆聚集偵測）
- `orderbook-hidden-ratio`: 隱藏掛單比例估算（成交量 vs 可見深度比較）
- `orderbook-competitor`: 競爭者自動放貸行為偵測（跟價模式、掛單頻率分析）

### Modified Capabilities

(無現有 spec 需修改)

## Impact

- 新增 `internal/lending/orderbook/` package（4 個 .go 檔 + 測試）
- 修改 `internal/lending/marketfeed/snapshot.go`：刪除內嵌分析，改呼叫 `orderbook/`
- 修改 `internal/lending/marketfeed/service.go`：整合 orderbook 分析結果至 snapshot
- `domain/snapshot.go` 可能新增型別（CompetitorInfo、DustFilterResult 等）
