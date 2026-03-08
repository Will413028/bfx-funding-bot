## 1. Domain + Bitfinex Client

- [x] 1.1 Create `internal/domain/earning.go` — `FundingEarning` struct (ID, Currency, Amount, Balance, Description, Timestamp)
- [x] 1.2 Add `GetFundingEarnings(ctx, apiKey, apiSecret, currency string, start, end time.Time)` method to `internal/bitfinex/client.go` — calls `POST /v2/auth/r/ledgers/{currency}/hist` with `category: 28`, parses response
- [x] 1.3 Write tests for `GetFundingEarnings` in `internal/bitfinex/client_test.go` — success with entries, empty response
- [x] 1.4 Update `docs/bitfinex-api-v2.md` — add Ledger endpoint documentation

## 2. Service Layer

- [x] 2.1 Create `internal/service/earnings.go` — `EarningsService` struct + `NewEarningsService` constructor (depends on bfx, apiKeyRepo, configRepo, cipher)
- [x] 2.2 Implement `GetEarnings(ctx, userID)` — check API key, parallel calls (credits + ledger 7d + ledger 30d), calculate estimated_daily_earning, weighted_apy, earnings_7d, earnings_30d
- [x] 2.3 Define response type `EarningsSummary` with all computed fields

## 3. Handler + Router + DI

- [x] 3.1 Create `internal/handler/earnings.go` — `EarningsHandler` with `Get` method
- [x] 3.2 Update `internal/handler/router.go` — add `GET /earnings` route
- [x] 3.3 Update `cmd/server/main.go` — fx.Provide `EarningsService` + `EarningsHandler`, pass to router

## 4. Tests

- [x] 4.1 Write `internal/service/earnings_test.go` — test: active credits with history, no API key, no credits, partial failure
- [x] 4.2 Write `internal/handler/earnings_test.go` — test: success response format

## 5. Verification

- [x] 5.1 Run `go build ./...` to verify compilation
- [x] 5.2 Run `go test ./...` to verify all tests pass
