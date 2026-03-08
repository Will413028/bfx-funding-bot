## Context

後端 Phase A-E + engine-integration 已完成，架構分層正確，但缺少 production 韌性機制。Bitfinex REST API 有速率限制（認證端點約 90 req/min），超過會被暫時封鎖。目前 lending engine 的多個 Worker 同時透過 `bitfinex/client.go` 發送請求，無全局保護。

現有程式碼的問題點：
- `cmd/server/main.go:134` — `time.Sleep(50 * time.Millisecond)` 等待 lending.Service.Start()
- `bitfinex/client.go` — 無 circuit breaker，無 rate limiter
- `service/apikey.go` WorkerManager interface — StartWorker/StopWorker 不接受 context
- `handler/health.go` — 只檢查 DB + Redis
- `repository/interfaces.go` ListByUser — 無分頁支援

## Goals / Non-Goals

**Goals:**
- Bitfinex API 呼叫具備熔斷保護（circuit breaker）和速率限制（rate limiter）
- 移除啟動流程中的 time.Sleep，改用確定性同步機制
- Service → lending 呼叫傳遞 context（含 request ID）
- Health check 反映 lending engine 實際狀態
- List endpoints 支援分頁，避免大量資料一次性載入

**Non-Goals:**
- OpenTelemetry / distributed tracing（延後至 Phase F）
- Bitfinex WebSocket 的 circuit breaker（ws.go 已有自動重連）
- 修改策略邏輯或 Worker 行為

## Decisions

### D1: Circuit Breaker — 使用 sony/gobreaker v2

**選擇**: `github.com/sony/gobreaker/v2`
**替代方案**: 自製 / `github.com/afex/hystrix-go`
**理由**: gobreaker 是 Go 社區最廣泛使用的 circuit breaker，API 簡潔，無外部依賴。hystrix-go 已停止維護。

**配置**:
- MaxRequests (half-open): 3
- Interval (closed 計數重置): 60s
- Timeout (open → half-open): 15s
- ReadyToTrip: 連續 5 次失敗

**實作位置**: 在 `bitfinex/client.go` 層級包裝，所有 REST 方法經過 circuit breaker。不在 Worker 層加，避免每個 Worker 獨立熔斷（應該全局熔斷）。

### D2: Rate Limiter — 使用 golang.org/x/time/rate

**選擇**: `golang.org/x/time/rate` (token bucket)
**理由**: Go 官方擴展庫，簡單可靠。Bitfinex 認證 API 限制約 90 req/min，設定 rate=1.0/s (60 req/min) 留 30% buffer。

**實作位置**: `bitfinex/client.go` 建構時注入 `*rate.Limiter`，每次 REST 呼叫前 `limiter.Wait(ctx)`。

**與 quota allocator 的分工**:
- `quota/allocator.go`: per-user 層級，控制單一用戶不超用
- `rate.Limiter`: 全局 HTTP 層級，保護不超過交易所上限

### D3: 移除 time.Sleep — ready channel

在 `lending.Service` 加一個 `readyCh chan struct{}`，`Start()` 中 pool 和 quota 初始化完成後 close(readyCh)。`startLendingEngine` 改用 `<-svc.Ready()` 等待。加 5 秒 timeout 防止卡死。

### D4: Context 傳遞 — interface 加 context.Context

`WorkerManager` 和 `ConfigReloader` 的方法簽名加入 `ctx context.Context` 作為第一個參數：
```go
type WorkerManager interface {
    StartWorker(ctx context.Context, userID string, cfg domain.StrategyConfig) error
    StopWorker(ctx context.Context, userID string) error
}

type ConfigReloader interface {
    ReloadConfig(ctx context.Context, userID string, cfg domain.StrategyConfig) error
}
```

`lending.Service` 的對應方法同步更新。目前 context 主要用於 logging（zap 從 context 取 request ID），未來可擴展為 tracing span。

### D5: Engine Health — lending.Service.Status()

新增 `lending.Service.Status() EngineStatus`，回傳：
```go
type EngineStatus struct {
    Running     bool
    WorkerCount int
    CircuitOpen bool // Bitfinex circuit breaker 是否開啟
}
```

`handler/health.go` 注入 lending engine status provider，在 health response 增加 `engine` 欄位。非 critical — engine 掛了不影響 CRUD API 的可用性，只影響放貸功能。

### D6: 分頁 — cursor-based (keyset pagination)

使用 `created_at` + `id` 作為 cursor（避免 OFFSET 的效能問題）。

Query 參數：`?limit=20&after=<cursor>`，cursor 是 base64 編碼的 `created_at|id`。

Response 增加 `pagination` 欄位：
```json
{
  "data": [...],
  "pagination": {
    "next_cursor": "...",
    "has_more": true
  }
}
```

預設 limit=20，最大 limit=100。

## Risks / Trade-offs

- **[Circuit breaker 誤觸發]** → 設定 ReadyToTrip 需要連續 5 次失敗，單次網路抖動不會觸發。半開狀態允許 3 個試探請求。
- **[Rate limiter 影響多用戶公平性]** → 目前單機部署，全局 limiter 是正確粒度。多機部署時需改為 Redis-based distributed limiter。
- **[Context 參數 breaking change]** → 修改 WorkerManager/ConfigReloader interface 會影響所有使用方。但使用方只有 service/apikey.go 和 service/config.go，影響範圍可控。
- **[分頁 cursor 不穩定]** → 如果 created_at 有重複值，cursor 跳頁可能不準。加入 `id` 作為 tiebreaker 解決。
