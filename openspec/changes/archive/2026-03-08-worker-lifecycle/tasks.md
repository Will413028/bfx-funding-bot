## 1. Lifecycle State

- [x] 1.1 Create `lending/worker/lifecycle.go` — WorkerState type (atomic), state constants, State() method
- [x] 1.2 Create `lending/worker/lifecycle_test.go` — state transition tests

## 2. Worker Implementation

- [x] 2.1 Create `lending/worker/worker.go` — consumer-side interfaces (Strategy, DataFetcher, OfferExecutor), UserData type
- [x] 2.2 Implement LendingWorker struct + NewLendingWorker constructor
- [x] 2.3 Implement Run main loop (snapshot select, tick, config reload, context cancel)
- [x] 2.4 Implement tick method (fetch → build context → strategy → execute) with panic recovery
- [x] 2.5 Implement Stop, ReloadConfig, UserID, State methods

## 3. Tests

- [x] 3.1 Create `lending/worker/worker_test.go` — all spec scenarios with mock dependencies
- [x] 3.2 Run `go test -race ./internal/lending/worker/...` and verify all pass
