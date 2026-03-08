## Why

CRUD 層（auth、API Key、strategy config）已完成，但還無法與 Bitfinex 交互。需要封裝 Bitfinex REST API 作為放貸引擎的基礎，讓系統可以驗證 API Key 權限、查詢錢包餘額、提交/撤銷放貸掛單。這是 lending engine 和未來所有 Bitfinex 操作的前置條件。

## What Changes

- 新增 `internal/bitfinex/` package — 自行實作 Bitfinex REST API v2 client（不使用官方 SDK）
- 新增 `bitfinex/client.go` — REST API 封裝：HMAC 簽名、驗證權限、查詢錢包、提交/撤銷 Funding Offer、查詢 Active Credits
- 新增 `bitfinex/auth.go` — HMAC-SHA384 簽名邏輯（nonce + payload）
- 新增 `domain/offer.go` — FundingOffer、OfferParams 領域模型
- 新增 `domain/credit.go` — FundingCredit 領域模型
- 新增 `domain/wallet.go` — Wallet（funding wallet balance）領域模型
- MVP 只做 REST API，WebSocket（`ws.go`）留到 lending engine 時再實作

## Capabilities

### New Capabilities
- `bitfinex-rest-client`: Bitfinex REST API 封裝（驗證權限、錢包查詢、Funding Offer CRUD、Active Credits 查詢）
- `funding-domain-models`: 放貸相關領域模型（FundingOffer、FundingCredit、Wallet）

### Modified Capabilities
（無）

## Impact

- **無新增外部依賴** — 使用 Go stdlib `net/http` + `crypto/hmac` 自行實作
- **新增 package**: `internal/bitfinex/`（client.go、auth.go）
- **新增 domain types**: offer.go、credit.go、wallet.go
- **修改 main.go**: fx.Provide bitfinex.NewClient
- 不修改現有 API endpoints — bitfinex client 是 internal 依賴，不直接暴露 HTTP API
