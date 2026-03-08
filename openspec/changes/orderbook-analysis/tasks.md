## 1. Domain 型別擴充

- [x] 1.1 在 `domain/snapshot.go` 新增 `WallType` 欄位至 `WallPosition`、新增 `OrderBookAnalysis` struct
- [x] 1.2 在 `domain/snapshot.go` 新增 `CompetitorActivity` 欄位至 `MarketSnapshot`

## 2. Order Book Summary 遷移

- [x] 2.1 建立 `lending/orderbook/summary.go`：遷移 `computeOrderBookSummary` 為公開函式
- [x] 2.2 更新 `marketfeed/snapshot.go`：改呼叫 `orderbook.ComputeSummary`

## 3. Dust Filter

- [x] 3.1 建立 `lending/orderbook/dust.go`：`FilterDust(entries, opts)` — 動態 median 閾值 + 雙側獨立過濾
- [x] 3.2 建立 `lending/orderbook/dust_test.go`

## 4. Wall Detection

- [x] 4.1 建立 `lending/orderbook/wall.go`：`DetectWalls(entries, spread, opts)` — 單一巨單牆 + 分散牆偵測
- [x] 4.2 更新 `marketfeed/snapshot.go`：移除舊 `detectWallPositions`，改呼叫 `orderbook.DetectWalls`
- [x] 4.3 建立 `lending/orderbook/wall_test.go`

## 5. Hidden Ratio

- [x] 5.1 建立 `lending/orderbook/hidden.go`：`HiddenRatioEstimator` struct — 滑動窗口成交量追蹤 + ratio 計算
- [x] 5.2 建立 `lending/orderbook/hidden_test.go`

## 6. Competitor Detection

- [x] 6.1 建立 `lending/orderbook/competitor.go`：`CompetitorDetector` struct — 跟價偵測 + round-number 分析
- [x] 6.2 建立 `lending/orderbook/competitor_test.go`

## 7. 整合 + 驗證

- [x] 7.1 更新 `marketfeed/service.go` 的 `assembleSnapshot`：整合 orderbook 分析結果（dust filter → summary → walls → hidden → competitor）
- [x] 7.2 更新 `marketfeed/snapshot_test.go` 和 `service_test.go`：適配新的 orderbook 呼叫
- [x] 7.3 確認完整專案編譯通過 + 所有測試通過
