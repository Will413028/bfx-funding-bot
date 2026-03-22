## 1. 提取 Rate-Setting Helpers（Stage 1 依賴）

- [x] 1.1 `pricing.go` — 新增 `ComputeBaseRate(snap *domain.MarketSnapshot, cfg *domain.StrategyConfig) float64`，提取 FRR → MDC premium → regime → depth pressure → wall avoidance 邏輯
- [x] 1.2 `floor.go` — 新增 `ComputeFloorRate(snap *domain.MarketSnapshot, cfg *domain.StrategyConfig) float64`，提取三層 floor（opportunity cost, FRR relative, regime）取 max 邏輯
- [x] 1.3 `weekend.go` — 新增 `ComputeWeekendMultiplier(t time.Time) float64`，提取 day-of-week → multiplier 邏輯
- [x] 1.4 `calendar.go` — 新增 `ComputeCalendarMultiplier(t time.Time) (multiplier float64, maxPeriod int)`，提取月末/季度溢價 + period 縮短邏輯
- [x] 1.5 `lockup.go` — 新增 `ComputeLockupPremium(baseRate float64, period int, volatility float64, regime domain.RegimeType) (premium float64, adjustedPeriod int)`，提取鎖倉成本計算
- [x] 1.6 `queue.go` — 新增 `ComputeQueueDiscount(rate float64, book domain.OrderBookSummary) float64` + `DetectStaleOffers(offers []domain.FundingOffer, book domain.OrderBookSummary) []int64`，提取排隊折扣 + stale 偵測
- [x] 1.7 `impact.go` — 新增 `ComputeImpactMultiplier(amount float64, existingAmount float64, askDepth float64) (multiplier float64, maxAmount float64)`，提取 impact ratio 計算

## 2. 提取 Period + Structure Helpers（Stage 2-3 依賴）

- [x] 2.1 `period.go` — 新增 `ComputePeriod(regime domain.RegimeType, volatility float64, offeredRate float64, frr float64, cfg *domain.StrategyConfig) int`，提取 regime-driven period + rate scaling + vol discount
- [x] 2.2 `stagger.go` — 新增 `AdjustPeriodForStagger(basePeriod int, credits []domain.FundingCredit, cfg *domain.StrategyConfig) int`，提取到期分佈分析 + 最佳 period
- [x] 2.3 `allocation.go` — 新增 `ComputeTiers(available float64, regime domain.RegimeType) []TierConfig`（TierConfig: Ratio, RateMultiplier），提取餘額門檻 + regime cap 邏輯
- [x] 2.4 `splitting.go` — 新增 `ComputeSplits(amount float64, askDepth float64) int`，提取深度感知拆分數量計算

## 3. 提取 Final Adjustment Helpers（Stage 4 依賴）

- [x] 3.1 `noise.go` — 新增 `ApplyNoise(rate float64, amount float64, rateMin float64, rateMax float64) (float64, float64)`，提取 rate/amount 擾動 + 心理價位避讓
- [x] 3.2 `partial.go` — 新增 `ComputeFillAdjustment(amount float64, activeOffers []domain.FundingOffer, cfg *domain.StrategyConfig) float64` + `DetectResiduals(offers []domain.FundingOffer) []int64`，提取 fill proxy + residual 偵測

## 4. CompositeStrategy 實作

- [x] 4.1 新增 `strategy/composite.go` — 定義 `CompositeStrategy` struct，實作 `Apply(ctx) *DecisionResult`，按 4 階段 pipeline 呼叫所有 helpers
- [x] 4.2 新增 `strategy/composite.go` 的 `NewCompositeStrategy()` constructor
- [x] 4.3 修改 `lending/factory.go` — `BuildWorkerDeps` 改用 `strategy.NewCompositeStrategy()` 取代 `strategy.NewPricingStrategy()`

## 5. 測試

- [x] 5.1 新增 `strategy/composite_test.go` — 測試 guard checks（nil snapshot, flash freeze, low balance）
- [x] 5.2 新增 `strategy/composite_test.go` — 測試 Stage 1 rate resolution（floor 高於 pricing → floor 生效；rate 超過 max → 被 clamp）
- [x] 5.3 新增 `strategy/composite_test.go` — 測試 Stage 3 多 tier 多 split 的完整 offer 產出
- [x] 5.4 新增 `strategy/composite_test.go` — 測試 Stage 4 noise 後 amount < $50 的 offer 被移除
- [x] 5.5 新增 `strategy/composite_test.go` — 測試 cancels 整合（queue stale + partial residual）
- [x] 5.6 驗證各 helper 函數的單元測試（確認提取後 existing tests 仍然通過）
