## Context

`bitfinex.Client` 是全系統 singleton，內含單一 `rate.Limiter`（1 req/s, burst 5），所有用戶的 API 呼叫都經過同一個 limiter。現有的 `quota.Allocator` 實作了 per-user token bucket（計數型），但未接入 client 層，worker tick 前沒有 `Acquire()`，形同虛設。

每個 worker tick 需 3 次 REST 呼叫（balance + offers + credits），Dashboard/Earnings 也各需 3 次。10 個活躍用戶 = 30 req/tick for workers + 隨機 dashboard 查詢，全部被 1 req/s 序列化。

Bitfinex API rate limit 依 tier 不同，一般帳號約 10-90 req/min（REST），需查閱實際文件確認。

## Goals / Non-Goals

**Goals:**

- Per-user rate limiting：每個用戶有獨立的 `rate.Limiter`，互不干擾
- 平台級安全網：client 層保留一個高閾值的 rate limiter，防止整體超過 Bitfinex 平台限制
- 整合 quota.Allocator：worker tick 前先 `Acquire()` 確認 token 足夠，用完 `Release()`
- Dashboard/Earnings 也接入 per-user 限流

**Non-Goals:**

- 動態調整 Bitfinex API tier（由配置決定，不自動偵測）
- Per-endpoint 不同 rate limit（統一 per-user 限流）
- 前端 rate limiting 改動（那是 HTTP middleware 層，與本次無關）

## Decisions

### D1: 新增 `RateLimiterPool` 管理 per-user limiter

**選擇**: 在 `lending/quota/` package 新增 `RateLimiterPool` struct，持有 `sync.Map` of `userID → *rate.Limiter`。

**替代方案**:
- 擴展 `quota.Allocator` 直接內嵌 limiter → 職責不清，Allocator 是 token 計數，limiter 是速率控制
- 每個 fetcher/executor 自帶 limiter → 無法跨 fetcher/executor 共享同一用戶的限額

**理由**: 獨立 struct 職責清晰，可用於 fetcher、executor、dashboard service 任何需要呼叫 Bitfinex API 的場景。

### D2: Client 層改為平台級 rate limiter

**選擇**: 將 `bitfinex.Client` 的 limiter 從 `rate.NewLimiter(1.0, 5)` 改為可配置的平台級限制，預設 `rate.NewLimiter(15, 20)`（15 req/s, burst 20）。透過 `NewClient(httpClient, opts...)` functional options 注入。

**替代方案**:
- 完全移除 client 層 limiter → 失去最後一道安全網，萬一 per-user limiter 有 bug 就直接打爆 Bitfinex API
- 寫死在 client → 不同部署環境（不同 API tier）無法調整

**理由**: 保留安全網，但閾值足夠高，正常運作不會觸及。

### D3: Per-user limiter 注入路徑

**選擇**: `DepsFactory.BuildWorkerDeps()` 從 `RateLimiterPool.Get(userID)` 取得 limiter，注入 fetcher 和 executor。Dashboard/Earnings service 直接持有 `RateLimiterPool` 引用。

**理由**: 沿用現有 DI 模式，最小改動。

### D4: Quota Allocator 整合

**選擇**: Worker `Run()` 主迴圈在 fetch 前呼叫 `quota.Acquire(userID, 3)`（3 次 API call）。如果 acquire 失敗，跳過此 tick 並 log。fetch 完成後不需要 Release（token 代表的是「已消耗的 API 呼叫」，不是可歸還的資源）。

**替代方案**:
- 在 fetcher 內部 acquire → 粒度太細，3 個平行呼叫分別 acquire 可能導致部分成功
- 完全移除 Allocator → 失去全域總量控制能力

**理由**: tick 層 acquire 保證 3 次呼叫的 token 一次性確認，避免部分成功。

## Risks / Trade-offs

- **[Per-user limiter 記憶體]** → 每個用戶一個 `rate.Limiter`（~100 bytes），1000 用戶 = 100KB，可忽略。用戶 stop 後從 pool 移除。
- **[平台級 limiter 閾值不準]** → 預設值可能不符實際 Bitfinex tier。透過 config 可調，部署後觀察 429 回應率調整。
- **[Quota refill 間隔]** → 現有 `StartRefill` 用固定間隔清零。如果間隔太長，某些用戶可能長時間無法 acquire。保持現有 1 分鐘 refill。
- **[Dashboard 與 worker 共用 per-user limiter]** → 用戶同時看 Dashboard 和 worker 在跑，會互相競爭同一 limiter。這是正確行為：同一用戶的所有 API 呼叫確實應該共享限額。
