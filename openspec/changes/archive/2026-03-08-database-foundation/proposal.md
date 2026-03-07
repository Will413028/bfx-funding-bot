## Why

後端 API 基礎架構已完成（Gin + fx + zap），但尚無資料庫連線。所有業務功能（使用者認證、API Key 管理、策略設定、帳單）都需要 PostgreSQL 持久化。需要建立 Neon PostgreSQL 連線、Atlas migration 工具鏈、sqlc 程式碼生成、以及 repository 層的基礎架構，作為後續所有資料存取的地基。

## What Changes

- 引入 `pgx/v5` + `pgxpool` 建立 Neon PostgreSQL 連線池
- 建立 `infra/` 層管理 DB 連線生命週期（fx lifecycle）
- 設定 Atlas 宣告式 schema + 版本化 migration 工具鏈
- 建立 `users` 基礎 schema 作為第一張表（後續認證變更的前置條件）
- 設定 sqlc 程式碼生成（SQL → type-safe Go code）
- 建立 `repository/` 層架構：interfaces、postgres DAO、fx module 聚合
- 擴充 health check 加入 DB 連線狀態檢查
- 擴充 `appconfig.Config` 加入 `DatabaseURL` 欄位

## Capabilities

### New Capabilities

- `db-connection`: PostgreSQL 連線池建立、fx 生命週期管理、連線健康檢查
- `migration`: Atlas 宣告式 schema 定義、migration 生成與執行流程
- `repository-layer`: Repository interface 定義、sqlc 設定、postgres DAO 包裝層架構

### Modified Capabilities

- `health-check`: 加入 DB 連線狀態檢查（原本只回傳 `{"status": "ok"}`）
- `app-config`: 新增 `DatabaseURL` 必要欄位

## Impact

- **新增依賴**：`github.com/jackc/pgx/v5`
- **新增工具**：Atlas CLI（`atlas`）、sqlc CLI（`sqlc`）— 開發機安裝，非 Go 依賴
- **影響程式碼**：`internal/infra/`（新增）、`internal/repository/`（新增）、`internal/appconfig/`（擴充）、`internal/handler/health.go`（擴充）
- **影響部署**：Koyeb 環境變數需新增 `DATABASE_URL`
- **資料庫**：Neon 上建立初始 schema（`users` 表）
