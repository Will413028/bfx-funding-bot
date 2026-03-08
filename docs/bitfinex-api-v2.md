# Bitfinex REST API v2 Reference

## Base URL
`https://api.bitfinex.com`

## Authentication (v2)

### Headers
- `Content-Type: application/json`
- `bfx-apikey`: API Key
- `bfx-nonce`: nonce (millisecond timestamp * 1000, as string)
- `bfx-signature`: HMAC-SHA384 hex digest

### Signature Computation (v2)
```
signaturePayload = "/api/" + apiPath + nonce + JSON.stringify(body)
signature = HMAC-SHA384(signaturePayload, apiSecret).hexDigest()
```

### JavaScript Example (v2)
```javascript
const nonce = (Date.now() * 1000).toString()
let signature = `/api/${apiPath}${nonce}${JSON.stringify(body)}`
const sig = CryptoJS.HmacSHA384(signature, apiSecret).toString()

fetch(`https://api.bitfinex.com/${apiPath}`, {
  method: 'post',
  body: JSON.stringify(body),
  headers: {
    'Content-Type': 'application/json',
    'bfx-nonce': nonce,
    'bfx-apikey': apiKey,
    'bfx-signature': sig
  }
})
```

### Note: v1 vs v2 差異
- v1 headers: `X-BFX-APIKEY`, `X-BFX-PAYLOAD`, `X-BFX-SIGNATURE`（base64 payload）
- v2 headers: `bfx-apikey`, `bfx-nonce`, `bfx-signature`（path + nonce + body 串接）

---

## Endpoints

### Wallets (驗證 key + 查餘額)
- **POST** `/v2/auth/r/wallets`
- Response: array of arrays
  ```
  [
    [TYPE, CURRENCY, BALANCE, UNSETTLED_INTEREST, AVAILABLE_BALANCE, LAST_CHANGE, LAST_CHANGE_METADATA],
    ...
  ]
  ```
  - TYPE: "exchange" | "margin" | "funding"
  - 篩選 `TYPE == "funding"` 取得放貸錢包
- Rate limit: 90 req/min

### Submit Funding Offer
- **POST** `/v2/auth/w/funding/offer/submit`
- Request body:
  ```json
  {
    "type": "LIMIT",
    "symbol": "fUSD",
    "amount": "50",
    "rate": "0.00002",
    "period": 2,
    "flags": 0
  }
  ```
  - `symbol`: "f" + currency (e.g., "fUSD", "fUST")
  - `rate`: 日利率（decimal fraction，0.0001 = 0.01%/day）
  - `amount`: positive = offer, negative = bid
  - `period`: 2-120 days
- Response: `[MTS, TYPE, MSG_ID, null, [OFFER_ARRAY], CODE, STATUS, TEXT]`
- Offer array fields:
  ```
  [0]  ID
  [1]  SYMBOL
  [2]  MTS_CREATED
  [3]  MTS_UPDATED
  [4]  AMOUNT
  [5]  AMOUNT_ORIGINAL
  [6]  OFFER_TYPE
  [7-9] null
  [10] OFFER_STATUS  (v2 docs show FLAGS here, offer listing shows STATUS at [11])
  [11] STATUS (in offer listing response)
  ...
  [15] RATE  (in offer listing, index may vary in submit response)
  [16] PERIOD
  [17] NOTIFY
  [18] HIDDEN
  [19] null
  [20] RENEW
  ```

### Cancel Funding Offer
- **POST** `/v2/auth/w/funding/offer/cancel`
- Request body: `{ "id": 604393839 }`
- Response: `[MTS, "foc-req", null, null, [OFFER_ARRAY], null, "SUCCESS", null]`

### Active Funding Offers
- **POST** `/v2/auth/r/funding/offers/{Symbol}`
- Symbol: "fUSD", "fETH", etc. (omit for all)
- Response: array of offer arrays
  ```
  [
    [ID, SYMBOL, MTS_CREATED, MTS_UPDATED, AMOUNT, AMOUNT_ORIG, TYPE,
     null, null, FLAGS, STATUS, null, null, null, RATE, PERIOD,
     NOTIFY, HIDDEN, null, RENEW, null],
    ...
  ]
  ```
  - Index mapping: 0=ID, 1=SYMBOL, 2=MTS_CREATED, 3=MTS_UPDATED, 4=AMOUNT, 5=AMOUNT_ORIG, 6=TYPE, 10=STATUS, 15=RATE (日利率), 16=PERIOD, 20=RENEW

### Active Funding Credits
- **POST** `/v2/auth/r/funding/credits/{Symbol}`
- Response: array of credit arrays
  ```
  [
    [ID, SYMBOL, SIDE, MTS_CREATE, MTS_UPDATE, AMOUNT, FLAGS, STATUS,
     RATE_TYPE, null, null, RATE, PERIOD, MTS_OPENING, MTS_LAST_PAYOUT,
     NOTIFY, HIDDEN, null, RENEW, null, NO_CLOSE, POSITION_PAIR],
    ...
  ]
  ```
  - Index mapping: 0=ID, 1=SYMBOL, 2=SIDE, 3=MTS_CREATE, 4=MTS_UPDATE, 5=AMOUNT, 7=STATUS, 11=RATE, 12=PERIOD, 13=MTS_OPENING, 18=RENEW

### Ledger History (Funding Earnings)
- **POST** `/v2/auth/r/ledgers/{Currency}/hist`
- Currency: "fUSD", "fETH", etc.
- Request body:
  ```json
  {
    "category": 28,
    "start": 1709251200000,
    "end": 1709856000000,
    "limit": 2500
  }
  ```
  - `category`: 28 = Margin Funding Payment
  - `start`/`end`: millisecond timestamps
  - `limit`: max 2500 (default 25)
- Response: array of ledger entries
  ```
  [
    [ID, CURRENCY, null, MTS, null, AMOUNT, BALANCE, null, DESCRIPTION],
    ...
  ]
  ```
  - Index mapping: 0=ID, 1=CURRENCY, 3=MTS, 5=AMOUNT, 6=BALANCE, 8=DESCRIPTION
  - AMOUNT: positive = interest received
  - DESCRIPTION: "Margin Funding Payment on wallet funding"

## Error Response Format
```json
["error", 10114, "nonce: small"]
```
- Index 0: "error" string
- Index 1: error code (integer)
- Index 2: error message (string)
