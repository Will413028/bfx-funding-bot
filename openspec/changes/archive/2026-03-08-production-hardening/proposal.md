## Why

後端核心架構已完成（Phase A-E + engine-integration），但缺少 production 環境所需的韌性機制。Bitfinex API 無 circuit breaker 和 rate limiter，故障時所有 Worker 同時重試可能觸發封鎖；啟動流程靠 `time.Sleep` 等待初始化；health check 未涵蓋 lending engine；list endpoint 缺少分頁。這些問題需在部署前解決。

## What Changes

- 為 Bitfinex client 加入 circuit breaker（連續失敗時自動熔斷，避免雪崩）
- 為 Bitfinex client 加入全局 HTTP rate limiter（遵守交易所 API 速率限制）
- 移除 `startLendingEngine` 中的 `time.Sleep(50ms)`，改用 ready channel 同步
- WorkerManager / ConfigReloader interface 加入 `context.Context` 參數，傳遞 request ID
- Health check 擴展，涵蓋 lending engine 狀態（pool 啟動、marketfeed 連線）
- GET /executions、GET /billing 加入 cursor-based 分頁
- 低優先：評估 OpenTelemetry tracing 整合（可延後）

## Capabilities

### New Capabilities
- `api-resilience`: Bitfinex client circuit breaker + rate limiter
- `engine-health`: Lending engine health check 擴展
- `api-pagination`: List endpoints cursor-based 分頁

### Modified Capabilities

（無既有 spec 需修改）

## Impact

- `bitfinex/client.go` — 加入 circuit breaker + rate limiter 包裝
- `lending/service.go` — 加入 ready channel + Status() 方法
- `cmd/server/main.go` — 移除 time.Sleep，改用 ready signal
- `service/apikey.go`, `service/config.go` — interface 加入 context 參數
- `handler/health.go` — 擴展 health check response
- `handler/execution.go`, `handler/billing.go` — 加入分頁參數
- `repository/interfaces.go` — ListByUser 加入 cursor 參數
- `repository/postgres/query/execution.sql`, `billing.sql` — 分頁 SQL
- 新增依賴：`github.com/sony/gobreaker/v2`, `golang.org/x/time/rate`
