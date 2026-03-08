## 1. Signal Types

- [x] 1.1 建立 `domain/signal.go`：定義 SignalType enum（6 個信號源）
- [x] 1.2 定義 SignalValue struct（Type, Value, Confidence, Timestamp）
- [x] 1.3 定義 MDCResult struct（Score, DemandPressure, SupplyPressure, Timestamp）

## 2. Regime Types

- [x] 2.1 建立 `domain/regime.go`：定義 RegimeType enum（Contango, Backwardation, Neutral, Crisis）
- [x] 2.2 定義 RegimeParams struct（Volatility, TrendStrength, DemandSupplyRatio, Duration）

## 3. Snapshot Types

- [x] 3.1 建立 `domain/snapshot.go`：定義 RawMarketData struct
- [x] 3.2 定義 OrderBookSummary struct
- [x] 3.3 定義 WallPosition struct
- [x] 3.4 定義 MarketSnapshot struct（引用 MDCResult, RegimeType, RegimeParams, SignalValue, OrderBookSummary, WallPosition）

## 4. 驗證

- [x] 4.1 確認所有檔案編譯通過，無外部依賴（僅 Go 標準庫）
