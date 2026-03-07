## 1. 依賴安裝

- [x] 1.1 安裝 pgx/v5 (`github.com/jackc/pgx/v5`)

## 2. App Config 擴充

- [x] 2.1 在 `internal/appconfig/config.go` 新增 `DatabaseURL` 欄位（required，從 `DATABASE_URL` 讀取）

## 3. Infra 層 — DB 連線

- [x] 3.1 建立 `internal/infra/di.go`（fx Module 聚合）
- [x] 3.2 建立 `internal/infra/postgres.go`（`NewPostgresPool`: pgxpool 連線 + fx lifecycle close hook）

## 4. Domain 層 — User 型別

- [x] 4.1 在 `internal/domain/user.go` 定義 `User` struct 和 `UserStatus` 常數（active, suspended）

## 5. Atlas Schema + Migration

- [x] 5.1 建立 `schema/atlas.hcl`（Atlas 專案配置：data source、migration 目錄）
- [x] 5.2 建立 `schema/schema.hcl`（宣告式 schema：users 表定義）
- [x] 5.3 用 Atlas CLI 產生初始 migration（`atlas migrate diff`）

## 6. sqlc 設定 + 程式碼生成

- [x] 6.1 建立 `sqlc.yaml`（設定 engine: postgresql、sql_package: pgx/v5、輸入輸出路徑）
- [x] 6.2 建立 `internal/repository/postgres/query/user.sql`（CreateUser、GetUserByID、GetUserByEmail）
- [x] 6.3 執行 `sqlc generate` 產生 Go 程式碼到 `internal/repository/postgres/sqlc/`

## 7. Repository 層

- [x] 7.1 建立 `internal/repository/interfaces.go`（定義 `UserRepository` interface）
- [x] 7.2 建立 `internal/repository/postgres/user.go`（包裝 sqlc，實作 `UserRepository`，sqlc model ↔ domain 轉換）
- [x] 7.3 建立 `internal/repository/postgres/di.go`（fx Module 註冊 postgres repos）
- [x] 7.4 建立 `internal/repository/di.go`（fx Module 聚合 postgres sub-module）

## 8. Health Check 擴充

- [x] 8.1 修改 `internal/handler/health.go`（注入 `*pgxpool.Pool`，加入 DB ping 檢查）

## 9. fx 整合

- [x] 9.1 在 `cmd/server/main.go` 加入 `infra.Module` 和 `repository.Module`

## 10. Makefile

- [x] 10.1 建立 `Makefile`（targets: dev, build, migrate-diff, migrate-apply, sqlc）

## 11. 驗證

- [x] 11.1 確認 `go build ./cmd/server/` 編譯成功
- [x] 11.2 手動測試：設定 `DATABASE_URL` 後啟動，health check 回傳 `{"status": "ok"}`
- [x] 11.3 手動測試：不設定 `DATABASE_URL`，應用啟動失敗並報錯
