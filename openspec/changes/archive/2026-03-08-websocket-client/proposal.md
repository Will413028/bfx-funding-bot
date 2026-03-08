## Why

目前放貸引擎 (`engine/`) 採用 REST API 輪詢 (3 分鐘間隔) 獲取市場數據，延遲高、無法即時反映市場變化。Phase C/D 的進階策略模組（閃崩偵測、動態定價、Order Book 分析）需要即時串流數據。WebSocket Client 是整個進階放貸引擎的基礎設施。

## What Changes

- 新增 `bitfinex/ws.go`：WebSocket Client，支援公開頻道（ticker、book、trades）和認證頻道（offers、credits、wallets）
- 新增 `bitfinex/ws_types.go`：WebSocket 訊息解析型別
- 復用既有 `bitfinex/auth.go` 的 HMAC-SHA384 簽章，新增 WebSocket 認證 payload 生成
- 自動重連機制（exponential backoff）、heartbeat 監控、maintenance code 處理
- Dead-Man-Switch (DMS) 支援：斷線時自動取消掛單
- Channel ID 管理：維護 chanId → symbol/channel 映射表

## Capabilities

### New Capabilities

- `websocket-connection`: WebSocket 連線生命週期管理（連線、認證、重連、heartbeat、maintenance codes）
- `websocket-public-channels`: 公開頻道訂閱與數據解析（funding ticker、order book、trades）
- `websocket-auth-channels`: 認證頻道數據接收與解析（funding offers、credits、wallets、notifications）

### Modified Capabilities

（無既有 spec 需修改）

## Impact

- **新增檔案**: `bitfinex/ws.go`, `bitfinex/ws_types.go`, `bitfinex/ws_test.go`
- **修改檔案**: `bitfinex/auth.go`（新增 WebSocket auth payload 函式）
- **新增依賴**: `github.com/gorilla/websocket`（WebSocket client library）
- **後續影響**: 此 client 將被 Phase C 的 Market Feed Service 使用，提供即時市場數據
