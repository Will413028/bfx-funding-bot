## Why

後續 Phase B 的 MarketSnapshot 快取和 Pub/Sub 廣播都需要 Redis。目前沒有 Redis 連線基礎設施。先整合 Upstash Redis 連線 + health check，為後續功能打好基礎。

## What Changes

- 新增 `appconfig` 支援 `REDIS_URL` 環境變數
- 新增 `infra/redis.go`：Redis client 建立 + fx Lifecycle 管理
- 擴充 `infra/di.go`：註冊 Redis provider
- 擴充 `handler/health.go`：加入 Redis connectivity check
- 新增 `go-redis/v9` 依賴

## Capabilities

### New Capabilities

- `redis-connection`: Redis 連線初始化、fx 生命週期管理、health check 整合

### Modified Capabilities

- `health-check`: 加入 Redis connectivity 狀態

## Impact

- **程式碼**：`appconfig/config.go`、`infra/redis.go`（新增）、`infra/di.go`、`handler/health.go`
- **依賴**：`github.com/redis/go-redis/v9`（新增）
- **環境變數**：新增 `REDIS_URL`（required）
