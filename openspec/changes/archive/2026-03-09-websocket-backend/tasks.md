## 1. WS Token

- [x] 1.1 Create `auth/ws_token.go` — `WSTokenManager` with `Generate(ctx, userID) (string, error)` and `Validate(ctx, token) (string, error)` using Redis SET/GET+DEL, 30s TTL, crypto/rand 32-byte hex
- [x] 1.2 Create `handler/ws_token.go` — `POST /auth/ws-token` handler, JWT-protected, returns `{"token":"<hex>"}`

## 2. WebSocket Hub & Client

- [x] 2.1 Create `handler/ws.go` — Hub struct (register/unregister/broadcast channels, clients map), Run() goroutine, Close() for graceful shutdown
- [x] 2.2 Add Client struct — conn, send channel (buffered 16), userID, readPump() (pong handler + discard reads), writePump() (ping ticker 30s, pong deadline 60s, write from send channel)
- [x] 2.3 Add WS upgrade handler — validate ws-token from query param, upgrade with gorilla/websocket, register client in Hub
- [x] 2.4 Add snapshot subscription — goroutine in Hub subscribes to Redis `SnapshotPubSub`, wraps as `{"type":"snapshot","data":{...}}`, sends to broadcast channel

## 3. Integration

- [x] 3.1 Update `handler/router.go` — register `POST /auth/ws-token` (JWT-protected) and `GET /ws` (no JWT middleware) routes
- [x] 3.2 Update `handler/di.go` — wire Hub + WSTokenManager into DI, add fx lifecycle hook for Hub.Run() and Hub.Close()

## 4. Verification

- [x] 4.1 Run `go build ./...` — verify compilation
- [x] 4.2 Run `go test ./...` — verify existing tests pass
