## Why

前端需要一個統一的 API 取得使用者的 funding 狀態摘要 — wallet balance、active offers、active credits。目前 Bitfinex client 已實作相關方法（`GetFundingBalance`、`GetActiveFundingOffers`、`GetActiveFundingCredits`），但沒有 HTTP endpoint 暴露給前端。使用者無法看到 engine 正在做什麼。

## What Changes

- 新增 `GET /api/v1/dashboard` endpoint（JWT 保護），回傳使用者的 funding 狀態摘要
- 摘要內容包含：
  - Wallet balance（available / total）
  - Active funding offers 列表
  - Active funding credits 列表
  - Engine 狀態（是否有 verified key + config）
- 新增 Dashboard service 層，聚合多個 Bitfinex API 呼叫
- 需要解密使用者的 API secret 來呼叫 Bitfinex API

## Capabilities

### New Capabilities
- `dashboard-summary`: 提供使用者 funding 狀態摘要的 API endpoint，聚合 wallet、offers、credits 資料

### Modified Capabilities
（無）

## Impact

- 新增 `internal/handler/dashboard.go` — HTTP handler
- 新增 `internal/service/dashboard.go` — 聚合邏輯
- 修改 `internal/handler/router.go` — 註冊新路由
- 修改 `cmd/server/main.go` — DI wiring
- 依賴既有的 `bitfinex.Client`、`APIKeyRepository`、`crypto.AES`
