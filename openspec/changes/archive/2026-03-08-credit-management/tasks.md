## 1. Core Implementation

- [x] 1.1 Create `lending/execution/credit.go` — CreditManager struct with NewCreditManager constructor (FundingClient, KeyStore, Cipher, ExecutionLog, now func)
- [x] 1.2 Implement ProcessCredits(ctx, userID, credits, config) — expiry detection, auto-renew logic, RenewSummary

## 2. Tests

- [x] 2.1 Create `lending/execution/credit_test.go` — tests covering all spec scenarios (auto-renew, skip, config bounds, failure tolerance, API key error, empty input)
- [x] 2.2 Run `go test ./internal/lending/execution/...` and verify all pass
