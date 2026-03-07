## Context

後端 API 基礎架構已就位（Gin + fx + zap），需要加入資料庫層。部署環境：Koyeb（後端）+ Neon（PostgreSQL）。架構文件已定義了 `infra/` → `repository/` → `service/` 的分層模式，以及 Atlas + sqlc 工具鏈。

## Goals / Non-Goals

**Goals:**
- 建立 PostgreSQL 連線池，由 fx 管理生命週期
- 設定 Atlas migration 工具鏈（宣告式 schema → 版本化 migration）
- 設定 sqlc 程式碼生成（SQL → type-safe Go code）
- 建立 repository 層架構（interfaces + postgres DAO + fx module）
- 建立 `users` 表作為第一張 schema
- Health check 加入 DB ping

**Non-Goals:**
- Redis 連線（後續獨立變更）
- 完整的 CRUD repository 實作（僅建立架構和 users 表的基礎查詢）
- Row-Level Security（後續多租戶安全變更）
- 其他業務表（api_keys、configs、billing 等由各自業務變更建立）

## Decisions

### 1. PostgreSQL Driver：pgx/v5

**選擇**：`github.com/jackc/pgx/v5` + `pgxpool`
**替代方案**：database/sql + lib/pq
**理由**：pgx 是 Go 生態效能最佳的 PostgreSQL driver，原生支援 PostgreSQL 型別（UUID、JSONB、arrays）。pgxpool 提供連線池管理。sqlc 原生支援 `sql_package: "pgx/v5"` 生成 pgx 風格程式碼。Neon 完全相容 pgx。

### 2. Migration：Atlas 宣告式

**選擇**：Atlas（宣告式 schema → 自動 diff → 版本化 migration）
**替代方案**：golang-migrate、goose
**理由**：架構文件已決定。宣告式 schema（`schema.hcl`）描述「期望狀態」，Atlas 自動計算 diff 產生 migration SQL。比手寫 migration 更不易出錯，且 schema 變更有單一 source of truth。

### 3. Query 生成：sqlc

**選擇**：sqlc（手寫 SQL → 自動生成 type-safe Go code）
**替代方案**：GORM、sqlx
**理由**：架構文件已決定。sqlc 不引入 ORM 的隱式查詢和 N+1 問題。開發者掌握完整 SQL，同時享有 type safety。生成的 `Querier` interface 方便測試 mock。

### 4. infra 層只管連線

`infra/postgres.go` 只負責建立 `*pgxpool.Pool` 並註冊 fx 關閉 hook。不包含任何查詢邏輯。`repository/` 層透過 fx 注入 pool。

### 5. users 表設計

初始 `users` 表包含認證所需的最小欄位：

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`status` 使用 TEXT 而非 ENUM，避免 migration 時的 ALTER TYPE 問題。應用層用 Go const 約束。

## Risks / Trade-offs

- **[Neon cold start]** → 首次查詢可能有數百毫秒延遲。放貸引擎 24/7 運行後不會觸發 suspend。Health check 定期 ping 也能保持 warm。
- **[Atlas CLI 開發依賴]** → 開發者需安裝 `atlas` CLI，不是 Go 依賴。在 Makefile 中加入安裝指引和 `make migrate` 指令。
- **[sqlc CLI 開發依賴]** → 同上，需安裝 `sqlc` CLI。Makefile 加入 `make sqlc` 指令。
- **[Neon sslmode=require]** → pgx 連線字串必須包含 `sslmode=require`，否則連線會被拒絕。在 appconfig 驗證中提示。
