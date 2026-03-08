# Bitfinex WebSocket API v2 Reference

## Endpoints

| 用途 | URL |
|------|-----|
| Public channels | `wss://api-pub.bitfinex.com/ws/2` |
| Authenticated channels | `wss://api.bitfinex.com/ws/2` |

### Rate Limits

- Public: 20 connections/min
- Authenticated: 5 connections/15s
- 每條連線最多 30 個 public subscription（含 authenticated 則上限 25）

---

## Connection Lifecycle

### 1. 連線後收到 info event

```json
{
  "event": "info",
  "version": 2,
  "serverId": "uuid",
  "platform": { "status": 1 }
}
```

- `platform.status`: 1 = operational, 0 = maintenance

### 2. Heartbeat

- 伺服器每 **15 秒** 對每個 channel 發送 heartbeat
- Format: `[CHANNEL_ID, "hb"]`
- 客戶端不需回應，但應監控偵測斷線

### 3. Maintenance / Reconnect Codes

| Code | 說明 | 建議動作 |
|------|------|---------|
| 20051 | Reconnection requested | 重新連線 |
| 20060 | Entering maintenance | 暫停活動，等待 20061 |
| 20061 | Maintenance ended | 重新連線 + 重新訂閱 |

Format: `{"event": "info", "code": 20051}`

---

## Public Channels

### Funding Ticker

訂閱：
```json
{"event": "subscribe", "channel": "ticker", "symbol": "fUSD"}
```

確認回應：
```json
{
  "event": "subscribed",
  "channel": "ticker",
  "chanId": 1,
  "symbol": "fUSD"
}
```

資料格式（Funding ticker 有 16 個欄位）：
```
[CHANNEL_ID, [
  FRR,               // [0]  Flash Return Rate (日利率)
  BID,               // [1]  Best bid rate
  BID_PERIOD,        // [2]  Bid period (days)
  BID_SIZE,          // [3]  Bid size
  ASK,               // [4]  Best ask rate
  ASK_PERIOD,        // [5]  Ask period (days)
  ASK_SIZE,          // [6]  Ask size
  DAILY_CHANGE,      // [7]  日變化（絕對值）
  DAILY_CHANGE_PERC, // [8]  日變化（百分比）
  LAST_PRICE,        // [9]  最後成交利率
  VOLUME,            // [10] 24h 成交量
  HIGH,              // [11] 24h 最高
  LOW,               // [12] 24h 最低
  _PLACEHOLDER,      // [13]
  _PLACEHOLDER,      // [14]
  FRR_AMOUNT_AVAIL   // [15] FRR 可用金額
]]
```

- FRR = Flash Return Rate，過去一小時的平均固定利率
- Rate 單位 = 日利率 decimal（0.0001 = 0.01%/day = 3.65%/year）

### Funding Order Book

訂閱：
```json
{
  "event": "subscribe",
  "channel": "book",
  "symbol": "fUSD",
  "prec": "P0",
  "len": "25"
}
```

- `prec`: 精度 P0 (5位有效數字) ~ P4 (1位)
- `len`: 深度 1 / 25 / 100 / 250

Snapshot（訂閱後首次）：
```
[CHANNEL_ID, [
  [RATE, PERIOD, COUNT, AMOUNT],
  [RATE, PERIOD, COUNT, AMOUNT],
  ...
]]
```

Update（逐筆更新）：
```
[CHANNEL_ID, [RATE, PERIOD, COUNT, AMOUNT]]
```

**Funding Book 特殊規則：**
- `AMOUNT > 0`: Offer (lending side)
- `AMOUNT < 0`: Bid (borrowing side)
- `COUNT > 0`: 該價位有 COUNT 筆掛單，新增或更新
- `COUNT = 0`: 刪除該價位（`AMOUNT = 1` 表示 offer side, `AMOUNT = -1` 表示 bid side）

### Funding Trades

訂閱：
```json
{"event": "subscribe", "channel": "trades", "symbol": "fUSD"}
```

Snapshot：
```
[CHANNEL_ID, [[ID, MTS, AMOUNT, RATE, PERIOD], ...]]
```

Update：
```
[CHANNEL_ID, "fte", [ID, MTS, AMOUNT, RATE, PERIOD]]
```

- Type: `"fte"` = funding trade executed, `"ftu"` = funding trade updated
- `AMOUNT > 0`: lend, `AMOUNT < 0`: borrow
- `RATE`: 日利率
- `PERIOD`: 天數

---

## Authentication

### Auth Payload

連線後發送 auth event：
```json
{
  "event": "auth",
  "apiKey": "YOUR_API_KEY",
  "authSig": "SIGNATURE_HEX",
  "authNonce": "NONCE_STRING",
  "authPayload": "AUTH{NONCE}",
  "dms": 4,
  "filter": ["funding-FOF", "funding-FCS", "wallet"]
}
```

### Signature Computation

```
nonce = string(timestamp_ms * 1000)       // 必須遞增，不可重複
payload = "AUTH" + nonce
signature = HMAC-SHA384(payload, apiSecret).hexDigest()
```

**與 REST API 差異：**
- REST: `payload = "/api/" + apiPath + nonce + body`
- WebSocket: `payload = "AUTH" + nonce`

### Optional Parameters

| 參數 | 值 | 說明 |
|------|---|------|
| `dms` | `4` | Dead-Man-Switch：斷線時自動取消所有掛單 |
| `filter` | string array | 過濾特定頻道，減少流量 |

### Filter 值（Funding 相關）

| Filter | 說明 |
|--------|------|
| `funding-FOF` | Funding Offers |
| `funding-FCS` | Funding Credits |
| `funding-FLN` | Funding Loans |
| `wallet` | Wallet snapshots + updates |
| `notify` | Notifications |

### Auth Success

```json
{
  "event": "auth",
  "status": "OK",
  "chanId": 0,
  "userId": 12345,
  "auth_id": "uuid",
  "caps": { ... }
}
```

### Auth Failure

```json
{
  "event": "auth",
  "status": "FAILED",
  "chanId": 0,
  "code": 10100,
  "msg": "apikey: invalid"
}
```

---

## Authenticated Channels (chanId = 0)

所有 authenticated data 都在 `chanId = 0` 上，以 message type 區分。

### Funding Offers

| Type | 說明 |
|------|------|
| `fos` | Snapshot（所有 active offers） |
| `fon` | New offer |
| `fou` | Offer update |
| `foc` | Offer cancel |

Format:
```
[0, "fos", [[ID, SYMBOL, MTS_CREATED, MTS_UPDATED, AMOUNT, AMOUNT_ORIG, TYPE,
             null, null, FLAGS, STATUS, null, null, null, RATE, PERIOD,
             NOTIFY, HIDDEN, null, RENEW, RATE_REAL], ...]]

[0, "fon", [ID, SYMBOL, MTS_CREATED, MTS_UPDATED, AMOUNT, AMOUNT_ORIG, TYPE,
            null, null, FLAGS, STATUS, null, null, null, RATE, PERIOD,
            NOTIFY, HIDDEN, null, RENEW, RATE_REAL]]
```

Offer array index mapping（同 REST API）：
```
[0]  ID
[1]  SYMBOL (e.g. "fUSD")
[2]  MTS_CREATED
[3]  MTS_UPDATED
[4]  AMOUNT
[5]  AMOUNT_ORIG
[6]  TYPE ("LIMIT", "FRRDELTA")
[10] STATUS ("ACTIVE", "EXECUTED", "PARTIALLY FILLED", "CANCELED")
[14] RATE (日利率)
[15] PERIOD (天數)
[19] RENEW (0 or 1)
```

### Funding Credits

| Type | 說明 |
|------|------|
| `fcs` | Snapshot（所有 active credits） |
| `fcn` | New credit |
| `fcu` | Credit update |
| `fcc` | Credit close |

Format:
```
[0, "fcs", [[ID, SYMBOL, SIDE, MTS_CREATE, MTS_UPDATE, AMOUNT, FLAGS, STATUS,
             RATE_TYPE, null, null, RATE, PERIOD, MTS_OPENING, MTS_LAST_PAYOUT,
             NOTIFY, HIDDEN, null, RENEW, null, NO_CLOSE, POSITION_PAIR], ...]]
```

Credit array index mapping（同 REST API）：
```
[0]  ID
[1]  SYMBOL
[2]  SIDE
[5]  AMOUNT
[7]  STATUS
[11] RATE (日利率)
[12] PERIOD (天數)
[13] MTS_OPENING
[18] RENEW (0 or 1)
```

### Wallet Updates

| Type | 說明 |
|------|------|
| `ws` | Snapshot |
| `wu` | Update |

Format:
```
[0, "ws", [[TYPE, CURRENCY, BALANCE, UNSETTLED, AVAILABLE, LAST_CHANGE, META], ...]]
[0, "wu", [TYPE, CURRENCY, BALANCE, UNSETTLED, AVAILABLE, LAST_CHANGE, META]]
```

- TYPE: `"exchange"` / `"margin"` / `"funding"`
- 篩選 `TYPE == "funding"` 取得放貸錢包

### Notifications

| Type | 說明 |
|------|------|
| `n` | Notification |

Format:
```
[0, "n", [MTS, TYPE, MSG_ID, null, NOTIFY_INFO, CODE, STATUS, TEXT]]
```

- STATUS: `"SUCCESS"` / `"ERROR"` / `"FAILURE"`
- NOTIFY_INFO: 內含操作結果（如 offer submit 的回應）

### Funding Info Update

| Type | 說明 |
|------|------|
| `fiu` | Funding info update |

Format:
```
[0, "fiu", ["sym", "fUSD", [YIELD_LOAN, YIELD_LEND, DURATION_LOAN, DURATION_LEND]]]
```

- YIELD_LEND: 加權平均放貸利率
- DURATION_LEND: 加權平均放貸天數

---

## Subscription / Unsubscription

### Subscribe

```json
{"event": "subscribe", "channel": "ticker", "symbol": "fUSD"}
{"event": "subscribe", "channel": "book", "symbol": "fUSD", "prec": "P0", "len": "25"}
{"event": "subscribe", "channel": "trades", "symbol": "fUSD"}
```

### Unsubscribe

```json
{"event": "unsubscribe", "chanId": 1}
```

### Subscribe Response

```json
{
  "event": "subscribed",
  "channel": "ticker",
  "chanId": 1,
  "symbol": "fUSD"
}
```

### Unsubscribe Response

```json
{
  "event": "unsubscribed",
  "chanId": 1,
  "status": "OK"
}
```

---

## Error Response

```json
{"event": "error", "msg": "subscribe: invalid", "code": 10300}
```

常見錯誤碼：

| Code | 說明 |
|------|------|
| 10000 | Unknown event |
| 10100 | Authentication failure (invalid API key) |
| 10111 | Authentication failure (invalid signature) |
| 10114 | Nonce too small |
| 10300 | Subscription failure |
| 10301 | Already subscribed |
| 10400 | Unsubscription failure |

---

## 實作注意事項

### 自動重連策略

1. 監控 heartbeat（15s 未收到 → 視為斷線）
2. 收到 code 20051 → 立即重連
3. 收到 code 20060 → 暫停，等 20061 後重連
4. 使用 exponential backoff（1s, 2s, 4s, 8s, max 30s）
5. 重連後需重新訂閱所有 channel + 重新 authenticate

### Channel ID 管理

- 每次訂閱成功後，伺服器分配 `chanId`
- 後續資料以 `[chanId, data]` 格式發送
- 需維護 `chanId → symbol/channel` 的映射表
- 重連後 chanId 會改變

### Dead-Man-Switch (DMS)

- Auth 時設 `"dms": 4`
- 斷線後 Bitfinex 會自動取消該連線的所有掛單
- 對放貸 bot 很重要：避免斷線時掛單繼續生效

### 流量控制

- 建議用 `filter` 參數只訂閱需要的 authenticated channel
- 減少不必要的 trades/book 數據量
- 考慮使用較低精度的 order book（P1/P2）降低更新頻率
