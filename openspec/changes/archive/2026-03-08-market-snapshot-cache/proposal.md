## Why

Phase C 的 Market Feed Service 計算出 MarketSnapshot 後，需要一個共享快取讓 Dashboard API 讀取最新市場狀態，以及一個 Pub/Sub 機制讓多機部署時所有 Worker 實例都能接收即時快照更新。目前 Redis 基礎設施已就緒（`infra/redis.go`），但 `repository/redis/` 目錄是空的。

## What Changes

- 新增 `repository/redis/snapshot.go`：MarketSnapshot Redis 快取（JSON 序列化, TTL 控制）
- 新增 `repository/redis/pubsub.go`：Pub/Sub 廣播與訂閱 MarketSnapshot
- 新增 `repository/redis/di.go`：fx Module 提供 Redis repository bindings
- 修改 `repository/interfaces.go`：新增 SnapshotCache 和 SnapshotPubSub interface
- 修改 `repository/di.go`：整合 Redis module

## Capabilities

### New Capabilities

- `snapshot-cache`: Redis 快取 MarketSnapshot（Set/Get by symbol, TTL 管理）
- `snapshot-pubsub`: Redis Pub/Sub 廣播 MarketSnapshot（Publish/Subscribe 跨實例）

### Modified Capabilities

（無）

## Impact

- **新增檔案**: `repository/redis/snapshot.go`, `pubsub.go`, `di.go`
- **修改檔案**: `repository/interfaces.go`, `repository/di.go`
- **後續影響**: Phase C1 Market Feed Service 將使用 SnapshotCache.Set + SnapshotPubSub.Publish；Dashboard handler 將使用 SnapshotCache.Get 提供即時數據
