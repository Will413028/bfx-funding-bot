## 1. Domain 型別擴充

- [x] 1.1 `domain/snapshot.go` — MarketSnapshot 新增 `RatePercentile float64` + `BookGaps []RateGap` + `RateGap` struct
- [x] 1.2 `domain/decision.go` — DecisionContext 新增 `AvgGapMinutes float64` 欄位
- [x] 1.3 `domain/signal.go` — 新增 `SignalRatePercentile` SignalType 常數

## 2. G16 Order Book Gap Detection

- [x] 2.1 新增 `orderbook/gap.go` — `DetectGaps` + `FindGapForRate`
- [x] 2.2 新增 `orderbook/gap_test.go` — 5 tests
- [x] 2.3 `strategy/pricing.go` — `ComputeBaseRate` 加入 gap detection

## 3. S4 RatePercentile Signal

- [x] 3.1 新增 `signal/percentile.go` — ring buffer + percentile rank
- [x] 3.2 新增 `signal/percentile_test.go` — P75、P25、insufficient data
- [x] 3.3 `marketfeed/snapshot.go` — 提取 RatePercentile + BookGaps

## 4. S10 Mean-Reversion P(higher)

- [x] 4.1 新增 `strategy/meanreversion.go` — `PHigherRate()` + `normalCDF()`
- [x] 4.2 新增 `strategy/meanreversion_test.go` — 6 tests
- [x] 4.3 `strategy/composite.go` — floor 階段 PHigherRate 調整 urgency

## 5. M2 Gap Cost Tracking

- [x] 5.1 `worker/worker.go` — gap EMA tracking + ctx.AvgGapMinutes 注入
- [x] 5.2 `strategy/composite.go` — period gap cost penalty

## 6. G14 Auto-Renew Re-pricing

- [x] 6.1 `execution/credit.go` — config midpoint re-pricing
- [x] 6.2 `execution/credit_test.go` — 驗證新行為

## 7. 整合到 Composite Pipeline

- [x] 7.1 `strategy/pricing.go` — G16 gap detection
- [x] 7.2 `strategy/composite.go` — S4 percentile 影響 period
- [x] 7.3 `strategy/composite.go` — S4 低 percentile 降低 deploymentRatio

## 8. 驗證

- [x] 8.1 `go build ./...` 編譯通過
- [x] 8.2 `golangci-lint run ./internal/lending/...` 通過
- [x] 8.3 `go test ./internal/lending/...` 全部通過（9/9 packages）
