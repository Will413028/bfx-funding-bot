## 1. RateLimiterPool

- [x] 1.1 在 `lending/quota/` 新增 `limiter_pool.go`：`RateLimiterPool` struct，持有 `sync.Map`，提供 `Get(userID) *rate.Limiter`、`Remove(userID)`，constructor `NewRateLimiterPool(ratePerSec float64, burst int)`
- [x] 1.2 新增 `limiter_pool_test.go`：測試 Get 建立/復用、Remove 刪除、concurrent access

## 2. Client 層平台級 rate limiter

- [x] 2.1 在 `bitfinex/client.go` 新增 `ClientOption` functional options pattern，加入 `WithRateLimit(ratePerSec float64, burst int)` option
- [x] 2.2 將 `NewClient` 改為 `NewClient(httpClient, opts ...ClientOption)`，預設 rate=15, burst=20
- [x] 2.3 更新 `cmd/server/main.go` 和所有 `NewClient` 呼叫點以匹配新簽名
- [x] 2.4 更新 `bitfinex/client_test.go` 中 NewClient 呼叫

## 3. Fetcher 整合 per-user limiter

- [x] 3.1 `lending/fetcher.go`：`userDataFetcher` 新增 `limiter *rate.Limiter` 欄位，`newUserDataFetcher` 接受 limiter 參數
- [x] 3.2 `FetchUserData` 的 3 個平行 goroutine 在 API 呼叫前各加 `f.limiter.Wait(ctx)`
- [x] 3.3 更新 `lending/factory.go`：`DepsFactory` 持有 `*quota.RateLimiterPool`，`BuildWorkerDeps` 內呼叫 `pool.Get(userID)` 取得 limiter 注入 fetcher

## 4. Executor 整合 per-user limiter

- [x] 4.1 `lending/execution/offer.go`：`OfferExecutor` 新增 `limiter *rate.Limiter` 欄位，`NewOfferExecutor` 接受 limiter 參數，`Execute` 呼叫前 `Wait(ctx)`
- [x] 4.2 `lending/execution/credit.go`：`CreditManager` 新增 `limiter *rate.Limiter` 欄位，`NewCreditManager` 接受 limiter 參數，`ProcessCredits` 呼叫前 `Wait(ctx)`
- [x] 4.3 `lending/execution/interest.go`：`InterestCollector` 新增 `limiter *rate.Limiter` 欄位，`NewInterestCollector` 接受 limiter 參數，`CheckAndReinvest` 呼叫前 `Wait(ctx)`
- [x] 4.4 更新 `lending/factory.go`：`BuildWorkerDeps` 將 limiter 注入 executor（透過 executorAdapter）
- [x] 4.5 更新 executor 單元測試以匹配新的 constructor 簽名

## 5. Dashboard / Earnings 整合 per-user limiter

- [x] 5.1 `service/dashboard.go`：`DashboardService` 新增 `limiterPool *quota.RateLimiterPool` 欄位，`NewDashboardService` 接受 pool 參數，`GetSummary` 3 個 goroutine 內加 `pool.Get(userID).Wait(ctx)`
- [x] 5.2 `service/earnings.go`：`EarningsService` 新增 `limiterPool *quota.RateLimiterPool` 欄位，`NewEarningsService` 接受 pool 參數，`GetEarnings` goroutine 內加 `pool.Get(userID).Wait(ctx)`
- [x] 5.3 更新 `service/dashboard_test.go` 和 `service/earnings_test.go` 以匹配新簽名
- [x] 5.4 更新 `handler/dashboard_test.go` 和 `handler/earnings_test.go` 以匹配新簽名

## 6. Quota Allocator 整合

- [x] 6.1 `lending/worker/worker.go`：`Run` 主迴圈 tick 開始時呼叫 `quota.Acquire(userID, 3)`，失敗則 skip tick 並 log warning
- [x] 6.2 更新 `worker.Deps` 或 `worker.Worker` 持有 quota allocator 引用
- [x] 6.3 `lending/service.go`：`StartWorker` 呼叫 `pool.Get(userID)` 確保 limiter 存在，`StopWorker` 呼叫 `pool.Remove(userID)`

## 7. DI Wiring

- [x] 7.1 `cmd/server/main.go`：建立 `quota.RateLimiterPool`（rate=2.0, burst=6）並注入 `DepsFactory`、`DashboardService`、`EarningsService`
- [x] 7.2 確認 `go build ./...` 通過
- [x] 7.3 確認 `go test ./...` 全部通過
