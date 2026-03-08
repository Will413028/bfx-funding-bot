## 1. Core Implementation

- [x] 1.1 Create `lending/execution/batch.go` — CreditBatch type + BatchCredits function (grouping, merging, weighted avg, period max, config bounds, split)
- [x] 1.2 Ensure expiresAt calculation consistent with credit.go (openedAt + period days)

## 2. Tests

- [x] 2.1 Create `lending/execution/batch_test.go` — tests for all spec scenarios
- [x] 2.2 Run `go test ./internal/lending/execution/...` and verify all pass
