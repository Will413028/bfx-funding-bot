# bfx-funding-bot

Bitfinex 自動放貸 SaaS 平台。

## 專案結構

- `backend/` — Go 1.25 後端（Gin + fx + zap）
- `frontend/` — Next.js 16 前端
- `openspec/` — OpenSpec 變更管理
- `backend_architecture.md` — **架構設計 source of truth**（功能規劃、分層架構、DB schema、API endpoints）

## 部署架構

| 服務 | 平台 |
|------|------|
| Frontend | Vercel |
| Backend | Koyeb (Docker) |
| Database | Neon (Serverless PostgreSQL) |
| Cache | Upstash (Serverless Redis) |

## 指令執行目錄

不同工具必須在正確的目錄下執行：

| 工具 | 執行目錄 | 範例 |
|------|----------|------|
| `atlas migrate diff` | `backend/schema/` | `cd backend/schema && atlas migrate diff xxx --env neon` |
| `atlas migrate apply` | `backend/schema/` | `cd backend/schema && source ../../.env && atlas migrate apply --env neon` |
| `sqlc generate` | `backend/` | `cd backend && sqlc generate` |
| `go test` | `backend/` | `cd backend && go test ./...` |
| `openspec` | 專案根目錄 | `openspec status --change "xxx"` |

## 開發規範

### Atlas Migration

- **套用 migration 一律使用 `atlas migrate apply --env neon`，不可用 MCP 直接執行 SQL**
- `atlas.hcl` 設定 `revisions_schema = "public"` — Neon pooler 不支援獨立 schema 存 revision
- `atlas migrate diff` 用於生成 migration 檔案（需要 Docker 執行 dev DB）
- 執行前需 `source ../../.env` 或 `export DATABASE_URL=...`

### sqlc 工作流

1. 修改 `backend/internal/repository/postgres/query/*.sql`
2. 在 `backend/` 下執行 `sqlc generate`
3. **`backend/internal/repository/postgres/sqlc/` 目錄是自動產生的，不可手改**

### .env 注意事項

- **DATABASE_URL 必須用雙引號包裹** — 連線字串含 `&`（query string），zsh `source .env` 會把 `&` 解讀為背景執行
  ```
  # 正確
  DATABASE_URL="postgresql://...?sslmode=require&channel_binding=require"

  # 錯誤 — & 會被 zsh 解析
  DATABASE_URL=postgresql://...?sslmode=require&channel_binding=require
  ```

### Neon PostgreSQL

- Project ID: `lingering-resonance-64910611`
- 使用 pooler 連線（hostname 含 `-pooler`）
- sqlc 搭配 `pgx/v5` driver

### 三層架構

- `handler/` → `service/` → `repository/`（consumer-side interface pattern）
- fx 依賴注入：`repository/di.go` 用 `fx.Annotate` + `fx.As` 綁定 interface
- 錯誤處理：統一使用 `domain.AppError`
- 每個使用者限一筆 API Key 和一筆 Strategy Config（UNIQUE constraint on user_id）

### 測試慣例

- 每層用 in-memory mock repo 測試，不依賴 DB
- `assertAppErrorCode(t, err, "CODE")` helper 定義在 `service/user_test.go`，跨 test 檔案共用
- handler test 用 `httptest.NewRecorder` + `gin.TestMode`

### Commit 訊息格式

使用 emoji prefix：
- `✨ Feat:` 新功能
- `🐛 Fix:` 修 bug
- `🔧 Chore:` 工具/設定調整
- `📝 Docs:` 文件
- `🎉 Init:` 初始化
- `♻️ Refactor:` 重構
- `✅ Test:` 測試
- `🔥 Remove:` 移除程式碼或檔案
- `🚀 Deploy:` 部署
- `💄 Style:` UI / 樣式
- `👷 CI:` CI/CD
- `⚡️ Perf:` 效能優化
- `🩹 Patch:` 非關鍵小修正
