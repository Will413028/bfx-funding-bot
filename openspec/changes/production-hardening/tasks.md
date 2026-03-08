## 1. Bitfinex Client Resilience

- [x] 1.1 Add `github.com/sony/gobreaker/v2` and `golang.org/x/time/rate` dependencies
- [x] 1.2 Add circuit breaker + rate limiter to `bitfinex/client.go` — wrap all REST methods, config: 5 failures/open, 15s timeout, 1.0 req/s
- [x] 1.3 Add unit tests for circuit breaker behavior (open/half-open/close transitions)
- [x] 1.4 Add unit tests for rate limiter (throttle + context cancellation)

## 2. Deterministic Engine Startup

- [x] 2.1 Add `Ready() <-chan struct{}` method to `lending/service.go` — close channel after pool + quota init
- [x] 2.2 Modify `startLendingEngine` in `main.go` — replace `time.Sleep(50ms)` with `<-svc.Ready()` + 5s timeout
- [x] 2.3 Update `lending/service_test.go` to verify ready signal behavior

## 3. Context Propagation

- [x] 3.1 Update `WorkerManager` interface in `service/apikey.go` — add `ctx context.Context` to StartWorker/StopWorker
- [x] 3.2 Update `ConfigReloader` interface in `service/config.go` — add `ctx context.Context` to ReloadConfig
- [x] 3.3 Update `lending.Service` methods (StartWorker, StopWorker, ReloadConfig) to accept context
- [x] 3.4 Update callers in `service/apikey.go`, `service/config.go` to pass context
- [x] 3.5 Update `resumeWorkers` in `main.go` to pass context
- [x] 3.6 Update all affected tests (service + handler)

## 4. Engine Health Check

- [x] 4.1 Add `EngineStatus` struct + `Status()` method to `lending/service.go`
- [x] 4.2 Define `EngineHealthProvider` interface in `handler/health.go` — inject via fx
- [x] 4.3 Extend health response JSON to include `engine` field (running, worker_count, circuit_breaker)
- [x] 4.4 Wire `*lending.Service` as `EngineHealthProvider` in `main.go`
- [x] 4.5 Add health handler tests for engine status

## 5. Cursor-Based Pagination

- [x] 5.1 Add pagination helpers (`EncodeCursor`, `DecodeCursor`, `PaginationResponse`) in `handler/pagination.go`
- [x] 5.2 Update `ExecutionRepository.ListByUser` — add cursor + limit params, update SQL
- [x] 5.3 Update `BillingRepository.ListByUser` — add cursor + limit params, update SQL
- [x] 5.4 Update `service/execution.go` + `service/billing.go` to pass pagination params
- [x] 5.5 Update `handler/execution.go` + `handler/billing.go` — parse query params, return pagination response
- [x] 5.6 Run `sqlc generate` and verify
- [x] 5.7 Add pagination handler tests

## 6. Verification

- [x] 6.1 Run `go build ./...` + `go test -race ./...` — all pass
- [x] 6.2 Update `backend_architecture.md` — 反映 circuit breaker, rate limiter, pagination, engine health
