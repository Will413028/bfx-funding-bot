## Context

Redis (Upstash) 已透過 `infra/redis.go` 連線，返回 `*redis.Client`。`repository/redis/` 目錄存在但為空。架構文件規劃此目錄應包含 `snapshot.go` 和 `pubsub.go`。

Domain 型別 `MarketSnapshot`（含 MDCResult、RegimeType、SignalValue 等）已在 B2 定義完成。

## Goals / Non-Goals

**Goals:**
- 提供 MarketSnapshot 的 Redis 快取讀寫（per-symbol, 含 TTL）
- 提供 MarketSnapshot 的 Redis Pub/Sub 廣播/訂閱
- 遵循既有 consumer-side interface pattern
- 整合進 fx DI 框架

**Non-Goals:**
- 不實作 Market Feed Service（Phase C1）
- 不修改 Dashboard handler（後續 change 再整合）
- 不實作 Redis Streams（Pub/Sub 足夠 Phase B 需求）

## Decisions

### 1. 序列化格式：JSON

使用 `encoding/json` 序列化 MarketSnapshot。原因：
- 可讀性好，便於 debug
- MarketSnapshot 資料量小（幾 KB），效能不是瓶頸
- 不需要額外依賴

替代方案：
- Protocol Buffers：效能更好但需額外 schema 定義
- MessagePack：較小但可讀性差

### 2. Key 格式

- Cache: `market:snapshot:{symbol}` (e.g. `market:snapshot:fUSD`)
- Pub/Sub channel: `market:snapshot:updates`

### 3. TTL 策略

- 預設 TTL: 60 秒
- 呼叫端可自訂 TTL
- 過期後 Get 返回 nil（無快取），不回傳過期資料

### 4. Interface 設計

分為兩個 interface（職責分離）：
- `SnapshotCache`：快取讀寫（Set/Get/Delete）
- `SnapshotPubSub`：廣播訂閱（Publish/Subscribe/Close）

Subscribe 返回 Go channel，讓上層用 `for range` 消費。

### 5. fx DI 整合

新增 `repository/redis/di.go` 提供 `redis.Module`，在 `repository/di.go` 中透過 `fx.Options` 整合。

## Risks / Trade-offs

- **[Risk] Upstash Pub/Sub 在 serverless 環境下可能有延遲** → Phase B 僅做基礎建設，實際效能在 Phase C 整合時驗證
- **[Trade-off] Pub/Sub vs Streams** → Pub/Sub 較簡單但 fire-and-forget（斷線期間的訊息會遺失）；Streams 有 ack 機制但更複雜。Phase B 選 Pub/Sub，後續如需可靠交付再升級
