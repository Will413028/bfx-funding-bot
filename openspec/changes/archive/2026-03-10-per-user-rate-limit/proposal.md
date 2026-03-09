## Why

`bitfinex.Client` 是 singleton，所有用戶共用一個 `rate.Limiter`（1 req/s, burst 5）。多用戶場景下，每個 worker tick 需要 3 次 API 呼叫，Dashboard/Earnings 也各需 3 次，導致所有請求被全域序列化、延遲嚴重。現有的 `quota.Allocator` 已實作 per-user token bucket 但未接入 client 層，形同虛設。

## What Changes

- 將 `bitfinex.Client` 的全域 `rate.Limiter` (1 req/s) 替換為平台級 rate limiter（對應 Bitfinex API tier 限制，預設 10 req/s, burst 15）
- 新增 `RateLimiterPool`：管理 per-user `rate.Limiter` 實例，每用戶預設 2 req/s, burst 6
- `userDataFetcher` 和 `execution.OfferExecutor` 注入 per-user rate limiter，在每次 API 呼叫前先 `Wait()`
- `DashboardService` 和 `EarningsService` 的 Bitfinex API 呼叫也接入 per-user 限流
- 整合 `quota.Allocator`：worker tick 開始前先 `Acquire()`，用完 `Release()`，使 token 計數與 rate limiter 雙層保護
- **BREAKING**: `DepsFactory.BuildWorkerDeps` 簽名變更（新增 rate limiter 參數）

## Capabilities

### New Capabilities

- `per-user-rate-limit`: Per-user API rate limiting pool，管理每用戶的 `rate.Limiter` 生命週期

### Modified Capabilities

- `api-rate-limiting`: 從全域 1 req/s 改為平台級安全網 + per-user 雙層限流
- `lending-engine-core`: worker/fetcher/executor 整合 per-user rate limiter 與 quota allocator

## Impact

- `bitfinex/client.go` — 調高全域 rate limiter 為平台級限制
- `lending/quota/` — 新增 `RateLimiterPool` 或擴展現有 allocator
- `lending/fetcher.go` — 注入 per-user limiter，呼叫前 `Wait()`
- `lending/execution/` — offer/credit/interest executor 注入 per-user limiter
- `lending/factory.go` — 組裝 per-user limiter 進 worker deps
- `service/dashboard.go`, `service/earnings.go` — 接入 per-user 限流
- `cmd/server/main.go` — 調整 DI wiring
