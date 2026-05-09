# bfx-funding-bot

Bitfinex 自動放貸 SaaS 平台。

## 專案結構

- `backend/` — Go 1.25 後端（Gin + fx + zap）
- `frontend/` — Next.js 16 前端
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

## 開發規範

### 開發工作流（Superpowers）

**所有功能開發、bug 修復、重構使用 Superpowers 技能：**

1. `brainstorming` skill — 新功能前釐清需求與設計方向
2. `writing-plans` skill — 輸出實作計畫
3. `test-driven-development` skill — 實作前先寫測試
4. `executing-plans` skill — 依計畫逐步實作
5. `requesting-code-review` skill — 完成後驗證

規則：
- 每個功能必須包含單元測試，`go test ./...` 全過才能 commit
- 純文件修改（ROADMAP、strategy-journal 等）不需要走完整工作流

### 測試與品質（強制）

- **每個 change 必須包含對應的單元測試**，不可只寫程式不寫測試
- **套用 migration 一律使用 `atlas migrate apply --env neon`，不可用 MCP 直接執行 SQL**
- 後端架構、測試慣例、sqlc/Atlas 工作流詳見 `backend/CLAUDE.md`
- 前端測試指令、架構慣例詳見 `frontend/CLAUDE.md`

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
