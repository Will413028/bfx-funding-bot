## Context

目前 `bitfinex/client.go` 是純 REST client，透過 HTTP POST + HMAC-SHA384 認證呼叫 Bitfinex API。放貸引擎 MVP (`engine/engine.go`) 每 3 分鐘輪詢一次 REST API 獲取市場數據。

Phase C 的市場分析層（閃崩偵測、Order Book 消耗分析、VWAP 等）需要即時串流數據，REST 輪詢無法滿足。需要一個 WebSocket Client 提供：
- 公開市場數據串流（ticker、book、trades）
- 認證用戶數據串流（offers、credits、wallets）

參考文件：`docs/bitfinex-websocket-v2.md`

## Goals / Non-Goals

**Goals:**
- 提供穩定的 WebSocket 連線管理（自動重連、heartbeat 監控）
- 支援公開頻道訂閱（funding ticker、order book、trades）
- 支援認證頻道（funding offers、credits、wallets）
- 提供 Go channel 介面讓上層消費者接收即時數據
- 可測試（支援注入 mock WebSocket 連線）

**Non-Goals:**
- 不實作 Market Feed Service（Phase C1）
- 不實作 Redis 快取/廣播（Phase B3）
- 不實作 Domain Market Types（Phase B2，將在下個 change 處理）
- 不處理 trading channels（只關注 funding 相關）

## Decisions

### 1. WebSocket Library: gorilla/websocket

選擇 `github.com/gorilla/websocket`，Go 生態最成熟的 WebSocket library。

替代方案：
- `nhooyr.io/websocket`：API 更現代但社群較小
- `golang.org/x/net/websocket`：功能太基礎

### 2. 架構：單一 Client struct + event callback

```
WSClient
├── conn         *websocket.Conn
├── channels     map[int]ChannelInfo    // chanId → subscription info
├── handlers     EventHandlers          // callback functions
├── reconnect    backoff config
└── done         chan struct{}
```

使用 callback pattern（`EventHandlers` struct 含各事件型別的 callback function），而非 Go channel。原因：
- 多種事件型別（ticker、book、trades、offers、credits、wallets）用單一 channel 需要 type switch，不直覺
- Callback 允許上層選擇性註冊需要的事件
- 避免 channel buffer 滿時的 backpressure 問題

替代方案：
- Fan-out channel per event type：更 Go-idiomatic 但管理複雜
- Single channel + interface：需要 type assertion，效能略差

### 3. 認證：復用 auth.go 的 HMAC-SHA384，新增 WS payload

WebSocket 認證的 payload 格式與 REST 不同：
- REST: `payload = "/api/" + apiPath + nonce + body`
- WS:   `payload = "AUTH" + nonce`

在 `auth.go` 新增 `computeWSSignature(nonce, apiSecret string) string` 函式。

### 4. 重連策略：Exponential Backoff

- 初始等待：1s
- 最大等待：30s
- 因子：x2
- Heartbeat timeout：20s（伺服器每 15s 發送）
- 收到 code 20051 → 立即重連
- 收到 code 20060 → 暫停活動，等 20061 後重連
- 重連後自動重新訂閱所有 channel + 重新認證

### 5. Dead-Man-Switch (DMS)

認證時設 `"dms": 4`，確保斷線後 Bitfinex 自動取消該連線的所有掛單。對放貸 bot 至關重要——避免斷線時舊掛單繼續生效。

### 6. Filter 參數

認證時使用 filter 限制流量：
```json
{"filter": ["funding-FOF", "funding-FCS", "wallet", "notify"]}
```
只接收 funding 相關事件，減少不必要的數據傳輸。

## Risks / Trade-offs

- **[Risk] 網路不穩導致頻繁重連** → 使用 exponential backoff + 重連次數日誌監控
- **[Risk] Heartbeat timeout 設太短誤判斷線** → 設 20s（伺服器 15s 間隔 + 5s buffer）
- **[Risk] 高流量幣種 book 更新頻率過高** → 上層可用較低精度 (P1/P2) 或限制 `len`
- **[Trade-off] Callback vs Channel** → 選 callback 犧牲一些 Go-idiomatic 風格，換取更靈活的事件分派
- **[Trade-off] 單連線 vs 多連線** → 先用單連線，每連線最多 25 subscription（含 auth），足夠 Phase B/C 需求
