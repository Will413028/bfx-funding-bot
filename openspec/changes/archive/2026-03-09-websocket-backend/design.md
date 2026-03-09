## Context

後端已有完整的 MarketSnapshot pipeline：MarketFeed → Redis Pub/Sub (`market:snapshot:updates`) → Worker broadcast。Dashboard REST API (`GET /dashboard`) 從 Redis cache 拉取最新快照。現在需要 WebSocket 端點讓前端即時接收快照更新，取代 polling。

既有依賴：`gorilla/websocket`（Bitfinex client 已使用）、Redis Pub/Sub（`SnapshotPubSub` interface 已實作）。

## Goals / Non-Goals

**Goals:**
- WebSocket 升級端點 + 短期 token 認證
- Hub pattern 管理多連線，訂閱 Redis snapshot channel 廣播給所有已認證使用者
- Heartbeat（ping/pong）+ 斷線清理
- 優雅關閉（fx lifecycle）

**Non-Goals:**
- 不實作 per-user 過濾（所有使用者收到相同 MarketSnapshot）
- 不實作雙向通訊（前端不需要透過 WS 發送命令）
- 不實作 WS reconnect token refresh（前端負責重新取得 ws-token）
- 不實作 Worker 狀態推送（僅推送 MarketSnapshot）

## Decisions

### D1: 短期 WS Token（非 JWT query param）

WebSocket 無法在 URL 中安全傳遞 JWT（URL 會被記錄在 access log、proxy log）。方案：

- `POST /api/v1/auth/ws-token` 回傳一次性短期 token（30 秒有效）
- Token 為 random 32-byte hex string，存在 Redis（`ws:token:{token}` → user_id，TTL 30s）
- WS 連線時 `GET /api/v1/ws?token=xxx`，驗證後立即從 Redis 刪除

**替代方案**: 用 JWT 短期 token — 多一層簽名驗證但不需要 Redis。選用 Redis 方案因為已有 Redis infra，且 token 一次性使用更安全。

### D2: Hub Pattern

```
Hub (singleton)
├── register chan *Client
├── unregister chan *Client
├── broadcast chan []byte
└── clients map[*Client]bool

Client
├── hub *Hub
├── conn *websocket.Conn
├── send chan []byte (buffered 16)
├── userID string
└── readPump() / writePump()
```

Hub 啟動一個 goroutine 監聽 register/unregister/broadcast。另一個 goroutine 訂閱 Redis snapshot channel 並轉發到 broadcast channel。

### D3: Message Format

```json
{
  "type": "snapshot",
  "data": { ... MarketSnapshot JSON ... }
}
```

未來可擴充 type（如 `"worker_status"`、`"notification"`）。

### D4: Heartbeat

- Server 每 30 秒發 ping
- Client 需在 60 秒內回 pong，否則斷開
- 使用 gorilla/websocket 內建 ping/pong handler

### D5: 路由與認證

- `POST /api/v1/auth/ws-token` — JWT 保護（同其他 protected route）
- `GET /api/v1/ws?token=xxx` — 不經過 JWT middleware，由 handler 自行驗證 ws-token

### D6: 檔案結構

```
handler/
├── ws.go          — Hub + Client + readPump/writePump + WS upgrade handler
├── ws_token.go    — POST /auth/ws-token handler
└── router.go      — 新增路由註冊

auth/
├── ws_token.go    — WS token 生成 + 驗證 (Redis get-and-delete)
```

## Risks / Trade-offs

- [連線數] → 初期不設上限，MVP 階段使用者少。未來可加 per-user 連線限制
- [Redis 依賴] → WS token 和 snapshot 都依賴 Redis。Redis 不可用時 WS 無法建立連線，但不影響 REST API
- [廣播效率] → 所有使用者收到相同 snapshot JSON，不需 per-user 序列化。大量連線時 Hub broadcast 可能成為瓶頸，但 SaaS 初期規模不是問題
