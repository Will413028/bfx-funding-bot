## 1. Service Layer

- [x] 1.1 Create `internal/service/dashboard.go` — `DashboardService` struct with dependencies (`bitfinex.Client`, `APIKeyRepository`, `ConfigRepository`, `crypto.AES`), `NewDashboardService` constructor
- [x] 1.2 Implement `GetSummary(ctx, userID)` — check API key exists + verified, check config exists, set `engine_ready` flag, parallel Bitfinex API calls via errgroup (wallet, offers, credits), handle partial failures
- [x] 1.3 Define response types: `DashboardSummary`, `WalletSummary`, `OfferSummary`, `CreditSummary` in service or domain layer

## 2. Handler + Router

- [x] 2.1 Create `internal/handler/dashboard.go` — `DashboardHandler` with `Get` method, extract userID from JWT context, call service, return JSON response
- [x] 2.2 Update `internal/handler/router.go` — add `GET /dashboard` route under protected group

## 3. DI Wiring

- [x] 3.1 Update `cmd/server/main.go` — fx.Provide `DashboardService` + `DashboardHandler`, pass handler to `NewRouter`

## 4. Tests

- [x] 4.1 Write `internal/service/dashboard_test.go` — test: user with verified key + config (full data), user without API key (empty), user with unverified key (empty), partial Bitfinex failure (graceful)
- [x] 4.2 Write `internal/handler/dashboard_test.go` — test: success response format, unauthenticated request (401)

## 5. Verification

- [x] 5.1 Run `go build ./...` to verify compilation
- [x] 5.2 Run `go test ./...` to verify all tests pass
