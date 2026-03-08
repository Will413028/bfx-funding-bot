## 1. Core Implementation

- [x] 1.1 Create `lending/service.go` — Service struct, Dependencies, NewService constructor
- [x] 1.2 Implement Start (quota refill + block) and Stop (StopAll + cleanup)
- [x] 1.3 Implement StartWorker (quota + snapshot channel + pool.Start)
- [x] 1.4 Implement StopWorker (pool.Stop + quota remove + channel close)
- [x] 1.5 Implement ReloadConfig (delegate to pool.Reload)
- [x] 1.6 Implement BroadcastSnapshot (fan-out to all snapshot channels)

## 2. Tests

- [x] 2.1 Create `lending/service_test.go` — all spec scenarios with mock deps
- [x] 2.2 Run `go test -race ./internal/lending/...` and verify all pass
