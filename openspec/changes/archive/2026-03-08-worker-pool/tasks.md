## 1. Core Implementation

- [x] 1.1 Create `lending/worker/pool.go` — Worker interface, Pool struct, NewPool constructor
- [x] 1.2 Implement Start, Stop, StopAll, Reload, Count, Has methods
- [x] 1.3 Implement auto-cleanup goroutine for worker exit

## 2. Tests

- [x] 2.1 Create `lending/worker/pool_test.go` — all spec scenarios with mock Worker
- [x] 2.2 Run `go test -race ./internal/lending/worker/...` and verify all pass
