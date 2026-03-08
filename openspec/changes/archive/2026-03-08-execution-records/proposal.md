## Why

放貸引擎執行操作（掛單/撤單/成交/續約）後需要持久化紀錄，供用戶查詢操作歷史、分析放貸績效，也是未來帳單計算（A6 Billing）的數據來源。目前引擎只有即時操作但無歷史紀錄。

## What Changes

- 新增 `domain/execution.go` 定義 `ExecutionRecord` 領域型別
- 新增 `executions` DB table（Atlas schema + migration）
- 新增 `execution.sql` sqlc query 檔 + 生成程式碼
- 新增 `repository/postgres/execution.go` 實作 `ExecutionRepository`
- 擴充 `repository/interfaces.go` 加入 `ExecutionRepository` interface
- 新增 `service/execution.go` 提供歷史查詢業務邏輯
- 新增 `handler/execution.go` 提供 `GET /executions` API endpoint
- 在 router、fx wiring 中註冊新的 handler/service/repo

## Capabilities

### New Capabilities
- `execution-records`: 放貸操作歷史紀錄的持久化、查詢 API

### Modified Capabilities

（無）

## Impact

- DB schema 新增 `executions` table，需跑 Atlas migration
- 新增 API endpoint `GET /api/v1/executions`（JWT protected）
- `cmd/server/main.go` 新增 fx provider
- `handler/router.go` 新增路由
