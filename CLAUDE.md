# bfx-funding-bot

Bitfinex 自動放貸 SaaS 平台。

## 專案結構

- `backend_py/` — Python 3.13 後端（FastAPI + SQLAlchemy 2.0 async + httpx + Alembic）— **active**
- `frontend/` — Next.js 16 前端
- ~~`backend/` — Go 1.25 後端~~ — **已移除**（Phase 0 重寫至 `backend_py/` 後封存並刪除，2026-05-29；歷史見 git）
- `backend_py/ARCHITECTURE.md` — **後端架構 source of truth**（event-sourced execution、reconcile 骨幹、deployment reconciler、放貸演算法、event model、DB schema、safety、phases/部署、key invariants；含 mermaid 架構/資料流圖）
- Phase 5+ SaaS 多租戶/API/billing 願景見 `ROADMAP.md`（原 Go-era `backend_architecture.md` 已移除，藍本見 git）

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
| `pytest` | `backend_py/` | `cd backend_py && uv run pytest -m "not integration"` |
| `mypy` / `ruff` | `backend_py/` | `cd backend_py && uv run mypy src/ && uv run ruff check` |
| `alembic revision --autogenerate` | `backend_py/` | `cd backend_py && uv run alembic revision --autogenerate -m "..."` |
| `alembic upgrade head` | `backend_py/` | `cd backend_py && uv run alembic upgrade head` |
| `alembic check` | `backend_py/` | `cd backend_py && uv run alembic check`（驗證 metadata 與 DB 無 drift） |

備註：`backend_py/` 必須 cd 進去才會走 uv 管的 Python 3.13；從 repo root 直接跑會撞 pyenv 3.12 的 sqlalchemy。`.env` 是 repo root 的 symlink（worktree 重建後要 `ln -sf ../.env backend_py/.env`）。

Go `backend/`（atlas/sqlc/go test）已於 2026-05-29 移除，不再使用。

## 開發規範

### 開發工作流（Superpowers）

**所有功能開發、bug 修復、重構使用 Superpowers 技能：**

1. `brainstorming` skill — 新功能前釐清需求與設計方向
2. `writing-plans` skill — 輸出實作計畫
3. `test-driven-development` skill — 實作前先寫測試
4. `executing-plans` skill — 依計畫逐步實作
5. `requesting-code-review` skill — 完成後驗證

規則：
- 每個功能必須包含單元測試，`cd backend_py && uv run pytest -m "not integration"` 全過才能 commit
- 純文件修改（ROADMAP、strategy-journal 等）不需要走完整工作流

### 測試與品質（強制）

- **每個 change 必須包含對應的單元測試**，不可只寫程式不寫測試
- **套用 migration 一律使用 `cd backend_py && uv run alembic upgrade head`，不可用 MCP 直接執行 SQL**
- 後端架構詳見 `backend_py/ARCHITECTURE.md`（runtime 架構/資料流/放貸演算法）；測試慣例、SQLAlchemy/Alembic 工作流詳見 `backend_py/CLAUDE.md`（待建；目前散見各 module 註解）
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
