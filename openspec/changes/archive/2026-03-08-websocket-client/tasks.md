## 1. Dependencies & Types

- [x] 1.1 新增 `github.com/gorilla/websocket` 依賴
- [x] 1.2 建立 `bitfinex/ws_types.go`：定義 WebSocket 訊息型別（FundingTicker, BookEntry, FundingTrade, WSFundingOffer, WSFundingCredit, WSWallet, WSNotification, ChannelInfo, EventHandlers）

## 2. Auth 擴充

- [x] 2.1 在 `bitfinex/auth.go` 新增 `computeWSAuthPayload(nonce, apiSecret string) (payload, signature string)` 函式

## 3. 連線管理核心

- [x] 3.1 建立 `bitfinex/ws.go`：WSClient struct + NewWSClient constructor + Connect/Close 方法
- [x] 3.2 實作 readLoop：讀取 WebSocket 訊息、解析 JSON、路由到對應 handler
- [x] 3.3 實作 heartbeat 監控：20s timeout 偵測斷線
- [x] 3.4 實作自動重連：exponential backoff (1s→30s)、重連後重新認證 + 重新訂閱
- [x] 3.5 實作 maintenance code 處理：20051 (重連)、20060/20061 (暫停/恢復)

## 4. 認證

- [x] 4.1 實作 authenticate 方法：發送 auth event（含 DMS=4, filter, HMAC-SHA384 簽章）
- [x] 4.2 處理 auth response：成功/失敗回調

## 5. 公開頻道

- [x] 5.1 實作 SubscribeTicker / SubscribeBook / SubscribeTrades 方法 + channel ID 管理
- [x] 5.2 實作 ticker 訊息解析（16 欄位 FundingTicker）
- [x] 5.3 實作 book snapshot + update 訊息解析
- [x] 5.4 實作 trades snapshot + fte/ftu 訊息解析
- [x] 5.5 實作 Unsubscribe 方法

## 6. 認證頻道

- [x] 6.1 實作 funding offers 事件解析（fos/fon/fou/foc）
- [x] 6.2 實作 funding credits 事件解析（fcs/fcn/fcu/fcc）
- [x] 6.3 實作 wallet 事件解析（ws/wu, 過濾 TYPE=="funding"）
- [x] 6.4 實作 notification 事件解析（n, SUCCESS/ERROR/FAILURE）

## 7. 測試

- [x] 7.1 建立 `bitfinex/ws_test.go`：使用 httptest + gorilla/websocket 建立 mock WS server
- [x] 7.2 測試連線生命週期（connect、info event、close）
- [x] 7.3 測試認證流程（成功、失敗）
- [x] 7.4 測試公開頻道訂閱與數據解析（ticker、book、trades）
- [x] 7.5 測試認證頻道數據解析（offers、credits、wallets）
- [x] 7.6 測試自動重連（模擬斷線 → 重連 → 重新訂閱）
- [x] 7.7 測試 heartbeat timeout 觸發重連
