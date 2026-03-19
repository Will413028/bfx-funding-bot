## 1. Domain Types

- [x] 1.1 Create `domain/performance.go` — PerformanceRecord, AlphaSummary, TagAlpha, AdaptiveAdjustment types
- [x] 1.2 Add StrategyTags field to DecisionResult in `domain/decision.go`

## 2. Alpha Tracking Module

- [x] 2.1 Create `lending/tracking/alpha.go` — AlphaCalculator with Compute(actual, frr) and NewRecord() builder
- [x] 2.2 Create `lending/tracking/summary.go` — RollingSummary with Add(record) and Summarize(window) for 7-day aggregation
- [x] 2.3 Write tests: alpha computation, summary aggregation, per-regime breakdown

## 3. Attribution & Feedback

- [x] 3.1 Create `lending/tracking/attribution.go` — AttributionAnalyzer with ByTag(records) grouping
- [x] 3.2 Create `lending/tracking/feedback.go` — AdaptiveFeedback with Analyze(tagAlphas) → []AdaptiveAdjustment, capped at ±10%
- [x] 3.3 Write tests: per-tag grouping, positive/negative adjustment generation, ±10% cap, per-regime separation

## 4. Redis Storage

- [x] 4.1 Create `repository/redis/performance.go` — Store/List PerformanceRecords per user with 7-day TTL
- [x] 4.2 Wire into repository/di.go

## 5. Integration

- [x] 5.1 Worker tick: after execution, create PerformanceRecord and store to Redis
- [x] 5.2 Strategy modules: add strategy tag strings to DecisionResult

## 6. Verification

- [x] 6.1 Run full test suite — all pass
- [x] 6.2 Run golangci-lint — clean
- [x] 6.3 Update ROADMAP.md — mark G10 complete
