## 1. Domain 型別擴充

- [x] 1.1 `domain/snapshot.go` — MarketSnapshot 新增 `FRRTrend float64`、`CascadePhase string`、`WeekendRatio float64` 欄位
- [x] 1.2 `domain/signal.go` — 新增 `SignalFRRTrend` 和 `SignalRateSpike` SignalType 常數

## 2. G12 FRR Trend Tracking

- [x] 2.1 新增 `signal/frrtrend.go` — `FRRTrend` struct 含 shortEMA/longEMA，`Compute(frr float64) SignalValue`，EMA alpha 分別對應 30min 和 4hr
- [x] 2.2 新增 `signal/frrtrend_test.go` — 測試上升趨勢、下降趨勢、insufficient data
- [x] 2.3 `marketfeed/snapshot.go` — assembleSnapshot 從 signals 提取 FRRTrend 寫入 MarketSnapshot

## 3. M5 Rate Spike Detector

- [x] 3.1 新增 `signal/ratespike.go` — `RateSpikeDetector` struct 含 EMA_1h，`Compute(data *RawMarketData) SignalValue`，閾值 `spikeThreshold = 2.0`
- [x] 3.2 新增 `signal/ratespike_test.go` — 測試 spike 觸發、正常波動不觸發、insufficient data
- [x] 3.3 Signal source 通過 DI 註冊，snapshot 從 signals 提取值

## 4. GT6 Queue Discount Sigmoid

- [x] 4.1 `strategy/queue.go` — `ComputeQueueDiscount` 改用 smoothstep sigmoid，新增 `smoothstep()` helper，`maxQueueDiscount = 0.05`
- [x] 4.2 更新 `strategy/queue_test.go` — 驗證 sigmoid 平滑過渡

## 5. G15 Smart Wall Positioning

- [x] 5.1 `strategy/pricing.go` — `applyWallAvoidance` 邏輯改為 `rate = wallRate - minTickSize`，新增 `minTickSize = 0.00000001` 常數
- [x] 5.2 更新 `strategy/pricing_test.go` — 驗證 wall 附近定價為 `wallRate - 1tick`

## 6. S1 BestAsk-Relative Pricing

- [x] 6.1 `strategy/pricing.go` — `ComputeBaseRate` 重寫：primary = `bestAsk - tickOffset(MDC, regime)`，fallback = 原有 FRR-based。新增 `computeTickOffset()`
- [x] 6.2 `strategy/pricing.go` — 新增 `applyFRRTrend(rate, frrTrend float64) float64`：`rate *= (1.0 + frrTrend × 0.1)`
- [x] 6.3 更新 `strategy/pricing_test.go` — 測試 bestAsk 可用時的定價、bestAsk=0 時 fallback
- [x] 6.4 `strategy/composite.go` — 確認 `ComputeBaseRate` 呼叫仍正常（介面不變）

## 7. S5 Cascade Phase Response

- [x] 7.1 `signal/liquidation.go` — `LiquidationCascade` 新增 `CascadePhase()` 方法，根據 `triggeredAt` 和當前時間判斷 early/mid/late
- [x] 7.2 `marketfeed/snapshot.go` — CascadePhase 通過 MarketSnapshot 欄位傳遞
- [x] 7.3 `strategy/composite.go` — Stage 2 根據 `CascadePhase` 調整 period：early→Period.Min，mid→14d，late→return empty
- [x] 7.4 Cascade phase 行為通過 composite tests 驗證

## 8. S6 Dynamic Weekend Premium

- [x] 8.1 `strategy/weekend.go` — `ComputeWeekendMultiplier` 新增 `historicalRatio float64` 參數，有歷史數據時用 ratio，無則 fallback 固定值
- [x] 8.2 `strategy/composite.go` — 傳遞 `snap.WeekendRatio` 到 `ComputeWeekendMultiplier`
- [x] 8.3 更新 `strategy/weekend_test.go` — 測試歷史 ratio 生效 + fallback

## 9. 驗證

- [x] 9.1 `go build ./...` 編譯通過
- [x] 9.2 `golangci-lint run ./...` lending 模組通過（2 個既有 worker_test errcheck 非本次改動）
- [x] 9.3 `go test ./internal/lending/...` 全部通過（9/9 packages）
