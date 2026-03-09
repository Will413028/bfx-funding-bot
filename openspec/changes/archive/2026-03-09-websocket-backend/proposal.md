## Why

Dashboard 目前透過 REST API 拉取市場快照，使用者無法看到即時更新。後端已有完整的 MarketSnapshot pipeline（MarketFeed → Redis Pub/Sub → Worker），但缺少將快照推送到前端的 WebSocket 通道。F12 建立 WebSocket 後端，讓 F13 前端可以訂閱即時快照更新。

## What Changes

- 新增 `POST /api/v1/auth/ws-token` — 生成短期 WS 認證 token（30 秒有效），避免在 WS URL query string 中暴露長期 JWT
- 新增 `GET /api/v1/ws` — WebSocket 升級端點，透過 query param `?token=` 驗證短期 token
- 新增 WebSocket Hub — 管理所有連線，訂閱 Redis Pub/Sub snapshot channel，廣播 MarketSnapshot 給已認證使用者
- 每個連線支援 heartbeat（ping/pong），斷線自動清理

## Capabilities

### New Capabilities
- `websocket-hub`: WebSocket 連線管理（Hub pattern — register/unregister/broadcast），per-connection goroutine pair (read pump + write pump)，heartbeat 機制
- `ws-token`: 短期 WebSocket 認證 token 生成與驗證，獨立於主 JWT，30 秒有效期

### Modified Capabilities
_None — 新增端點不影響既有 REST API 行為_

## Impact

- **新增檔案**: `handler/ws.go`（WebSocket handler + Hub）, `handler/ws_token.go`（ws-token 端點）, `auth/ws_token.go`（token 生成/驗證邏輯）
- **修改檔案**: `handler/router.go`（註冊新路由）, `handler/di.go`（DI 註冊）
- **依賴**: `github.com/gorilla/websocket`（已存在，Bitfinex client 使用中）
- **Redis**: 訂閱既有 `market:snapshot:updates` channel，不需新增 channel
- **API**: 新增 2 個端點，不修改既有端點
