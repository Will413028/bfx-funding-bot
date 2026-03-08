## Why

使用者需要知道放貸賺了多少錢。目前 Dashboard API 只顯示即時狀態（wallet、active offers、credits），但沒有收益統計。沒有 earnings 資訊，使用者無法評估放貸策略的效果，也缺乏使用產品的動力。

## What Changes

- 新增 Bitfinex client 方法：`GetFundingEarnings` — 呼叫 Ledger API 取得 funding 利息收入歷史
- 新增 `FundingEarning` domain model
- 新增 `GET /api/v1/earnings` endpoint（JWT 保護），回傳：
  - 從 active credits 計算的即時每日預估收益和加權平均 APY
  - 從 Ledger API 取得的歷史累計收益（過去 7 天、30 天）
- 新增 `EarningsService` 聚合計算邏輯

## Capabilities

### New Capabilities
- `funding-earnings`: 計算並提供 funding 收益統計的 API endpoint — 即時預估收益 + 歷史累計收益

### Modified Capabilities
- `bitfinex-rest-client`: 新增 Ledger API 呼叫方法（`GetFundingEarnings`）

## Impact

- 新增 `internal/domain/earning.go` — FundingEarning domain model
- 修改 `internal/bitfinex/client.go` — 新增 `GetFundingEarnings` 方法
- 新增 `internal/service/earnings.go` — EarningsService
- 新增 `internal/handler/earnings.go` — HTTP handler
- 修改 `internal/handler/router.go` — 註冊新路由
- 修改 `cmd/server/main.go` — DI wiring
- 更新 `docs/bitfinex-api-v2.md` — Ledger endpoint 文件
