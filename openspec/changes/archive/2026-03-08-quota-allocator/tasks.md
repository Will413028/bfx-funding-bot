## 1. Core Implementation

- [x] 1.1 Create `lending/quota/allocator.go` — Allocator struct, NewAllocator, per-user entry type
- [x] 1.2 Implement Acquire, Release, SetUserQuota, RemoveUser, Remaining, Refill
- [x] 1.3 Implement StartRefill background goroutine (ctx-cancellable)

## 2. Tests

- [x] 2.1 Create `lending/quota/allocator_test.go` — all spec scenarios + concurrent test
- [x] 2.2 Run `go test -race ./internal/lending/quota/...` and verify all pass
