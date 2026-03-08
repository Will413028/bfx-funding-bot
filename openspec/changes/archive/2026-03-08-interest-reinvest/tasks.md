## 1. Core Implementation

- [x] 1.1 Create `lending/execution/interest.go` — InterestCollector struct + ReinvestSummary type + CheckAndReinvest method
- [x] 1.2 Implement reinvest logic: threshold check, amount capping, conservative params, ExecutionRecord

## 2. Tests

- [x] 2.1 Create `lending/execution/interest_test.go` — all spec scenarios
- [x] 2.2 Run `go test ./internal/lending/execution/...` and verify all pass
