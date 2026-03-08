## 1. Repository Layer

- [x] 1.1 Add `ListVerified(ctx)` method to `APIKeyRepository` interface — returns all API keys with exchange_status="verified" (including encrypted secret)
- [x] 1.2 Add sqlc query `ListVerifiedAPIKeys` and implement in `postgres/apikey.go`
- [x] 1.3 Run `sqlc generate` to regenerate code

## 2. Strategy Logic

- [x] 2.1 Create `internal/engine/strategy.go` — `ExecuteStrategy(ctx, bfx, apiKey, apiSecret, config, log)` function that implements the MVP lending loop: check balance → check active offers → cancel stale offers (>15min) → submit new offer if eligible
- [x] 2.2 Write `internal/engine/strategy_test.go` — test strategy logic with mock Bitfinex server: submit when balance available, skip when low balance, skip when active offer exists, cancel stale offers

## 3. Engine Core

- [x] 3.1 Create `internal/engine/engine.go` — Engine struct with dependencies (bfx, apiKeyRepo, configRepo, cipher, logger), NewEngine constructor, Start/Stop methods, tick loop with time.Ticker (3 min interval)
- [x] 3.2 Implement `tick(ctx)` — list verified API keys, for each: decrypt secret, get user config, call ExecuteStrategy, handle errors per-user with recover
- [x] 3.3 Write `internal/engine/engine_test.go` — test engine tick: processes users with verified keys + configs, skips users without config, handles per-user errors gracefully

## 4. DI Wiring

- [x] 4.1 Update `cmd/server/main.go` — fx.Provide engine.NewEngine, fx.Invoke engine start via lifecycle hook

## 5. Verification

- [x] 5.1 Run `go build ./...` to verify compilation
- [x] 5.2 Run `go test ./...` to verify all tests pass
