## 1. Regime Detector

- [x] 1.1 建立 `lending/signal/regime.go`：`RegimeDetector` struct，含 enter/exit threshold + hysteresis 狀態
- [x] 1.2 實作 `Detect` 方法：MDC score → regime 分類 + crisis 偵測
- [x] 1.3 實作 `RegimeParams` 計算：volatility EMA、trend、demand/supply ratio、duration

## 2. 測試

- [x] 2.1 建立 `lending/signal/regime_test.go`：contango / backwardation / neutral / crisis 分類
- [x] 2.2 測試 hysteresis：進入閾值 vs 退出閾值
- [x] 2.3 測試 cold start + duration tracking

## 3. 整合

- [x] 3.1 更新 `marketfeed/service.go`：整合 `RegimeDetector` + `MDCAggregator`，取代 Phase 3 placeholder
- [x] 3.2 確認完整專案編譯通過 + 所有測試通過
