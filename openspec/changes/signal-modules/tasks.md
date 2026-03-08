## 1. Book Consumption Signal

- [x] 1.1 建立 `lending/signal/book.go`：BookConsumption struct，追蹤 book depth 歷史
- [x] 1.2 實作 Compute：計算消耗速率，標準化輸出 [-1,+1]

## 2. Liquidation Cascade Signal

- [x] 2.1 建立 `lending/signal/liquidation.go`：LiquidationCascade struct，5 分鐘滑動窗口
- [x] 2.2 實作 Compute：大量成交偵測 + 觸發/退場 regression (1.0→0.7→0.3→0)

## 3. Momentum Signal (Dual VWAP)

- [x] 3.1 建立 `lending/signal/momentum.go`：Momentum struct，維護 fast/slow VWAP
- [x] 3.2 實作 Compute：fast VWAP (5min) vs slow VWAP (20min) 差值，tanh 標準化

## 4. Margin Usage Signal

- [x] 4.1 建立 `lending/signal/margin.go`：MarginUsage struct，追蹤 bid/ask depth 比例
- [x] 4.2 實作 Compute：bid/ask ratio 變化信號

## 5. Cross-Currency Signal

- [x] 5.1 建立 `lending/signal/crossccy.go`：CrossCurrency struct，追蹤利率趨勢
- [x] 5.2 實作 Compute：最近成交利率 vs 歷史平均趨勢

## 6. MDC Aggregator

- [x] 6.1 建立 `lending/signal/mdc.go`：MDCAggregator struct，含 base weights + decay λ
- [x] 6.2 實作 Aggregate：freshness decay + weight renormalization + tanh 壓縮
- [x] 6.3 實作 liquidation hard override + demand/supply pressure 分解

## 7. 測試

- [x] 7.1 建立 `lending/signal/book_test.go`
- [x] 7.2 建立 `lending/signal/liquidation_test.go`
- [x] 7.3 建立 `lending/signal/momentum_test.go`
- [x] 7.4 建立 `lending/signal/margin_test.go`
- [x] 7.5 建立 `lending/signal/crossccy_test.go`
- [x] 7.6 建立 `lending/signal/mdc_test.go`

## 8. 驗證

- [x] 8.1 確認完整專案編譯通過 + 所有測試通過
