## Why

平台以佣金制盈利——從用戶放貸利息中抽取一定比例作為服務費。需要計費系統紀錄每個計費週期的利息收入、佣金金額，供用戶查詢帳單明細，也是未來付費功能的基礎。

## What Changes

- 新增 `domain/billing.go` 定義 `BillingRecord`、`BillingPeriod` 領域型別
- 新增 `billing_records` DB table（Atlas schema + migration）
- 新增 `billing.sql` sqlc query 檔 + 生成程式碼
- 新增 `repository/postgres/billing.go` 實作 `BillingRepository`
- 擴充 `repository/interfaces.go` 加入 `BillingRepository` interface
- 新增 `service/billing.go` 提供帳單查詢 + 摘要
- 新增 `handler/billing.go` 提供 `GET /billing` API endpoint
- 在 router、fx wiring 中註冊

## Capabilities

### New Capabilities
- `billing`: 計費紀錄持久化、查詢 API、帳單摘要

### Modified Capabilities

（無）

## Impact

- DB schema 新增 `billing_records` table，需跑 Atlas migration
- 新增 API endpoint `GET /api/v1/billing`（JWT protected）
- `cmd/server/main.go` 新增 fx provider
- `handler/router.go` 新增路由
