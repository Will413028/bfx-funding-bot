# bfx-funding-bot

Bitfinex 自動放貸 SaaS 平台。

## 專案結構

- `backend/` — Python 3.13 後端（FastAPI + SQLAlchemy 2.0 async + httpx + Alembic）— **active**
- `frontend/` — Next.js 16 前端
- ~~Go 1.25 後端~~ — **已移除**（Phase 0 重寫為 Python 後封存並刪除，2026-05-29；歷史見 git）。Python 後端原名 `backend_py/`，2026-09-23 改名為 `backend/`，舊文件與 SDD 裡的 `backend_py` 指的就是現在的 `backend/`
- `backend/ARCHITECTURE.md` — **後端架構 source of truth**（event-sourced execution、reconcile 骨幹、deployment reconciler、放貸演算法、event model、DB schema、safety、phases/部署、key invariants；含 mermaid 架構/資料流圖）
- Phase 5+ SaaS 多租戶/API/billing 願景見 `ROADMAP.md`（原 Go-era `backend_architecture.md` 已移除，藍本見 git）

## 部署架構

應用程式與資料服務自托於 Oracle Cloud VM（單機 Docker stack，`docker-compose.bot.yml`）；外部服務包含 Bitfinex venue 與 Cloudflare R2 offsite backup。

| 服務 | 平台 |
|------|------|
| Frontend | VM `bfx-frontend`（Next.js standalone，Tailscale Funnel 443→`127.0.0.1:3001`；公開 `https://oci-a1.tail357fef.ts.net`） |
| Backend (bot + webapi) | VM（`bfx-bot` / `bfx-webapi`；webapi 內網限定，FE 經 docker 網路呼叫） |
| Database | 自托 Postgres 18（`bfx-postgres`，volume `bfx_pgdata`；roles：bot owner、`bfx_webapi`、`bfx_webauth`） |
| Cache | 自托 Redis 7（`bfx-redis`，volume `bfx_redisdata`；Better Auth secondaryStorage：session + rate-limit，ioredis） |

- 備份／WAL archive／isolated restore 依 `docs/runbooks/offsite-dr.md`；pgBackRest backup/status timer 定義在 `deploy/vm/systemd/`，實際啟用與健康狀態須查目標環境。
- 每週 attribution／G3 報告由 `bfx-weekly-report.timer` 執行 compose `weekly-report`（`--profile ops`）；操作前核對目前 unit、排程及輸出。Redis session 為 ephemeral。

## 指令執行目錄

不同工具必須在正確的目錄下執行：

| 工具 | 執行目錄 | 範例 |
|------|----------|------|
| `pytest` | `backend/` | `cd backend && uv run pytest -m "not integration"` |
| `mypy` / `ruff` | `backend/` | `cd backend && uv run mypy src/ && uv run ruff check` |
| `alembic revision --autogenerate` | `backend/` | `cd backend && uv run alembic revision --autogenerate -m "..."` |
| `alembic upgrade head` | `backend/` | `cd backend && uv run alembic upgrade head` |
| `alembic check` | `backend/` | `cd backend && uv run alembic check`（驗證 metadata 與 DB 無 drift） |

備註：`backend/` 必須 cd 進去才會走 uv 管的 Python 3.13；從 repo root 直接跑會撞 pyenv 3.12 的 sqlalchemy。`.env` 是 repo root 的 symlink（worktree 重建後要 `ln -sf ../.env backend/.env`）。

Go `backend/`（atlas/sqlc/go test）已於 2026-05-29 移除，不再使用。

## 開發規範

### 開發工作流

需求未決或架構取捨先設計討論；多步驟工作先整理計畫。明確且低風險的修正直接實作與受影響驗證；放貸／風控／DB 行為變更補回歸測試並 review。依任務需要選用可用的 skills，仍須遵守架構與驗證要求。

規則：
- 每個功能必須包含單元測試，`cd backend && uv run pytest -m "not integration"` 全過才能 commit
- 純文件修改（ROADMAP、strategy-journal 等）不需要走完整工作流

### 測試與品質（強制）

- **行為變更補對應的回歸測試**；純文件改動核對指令、路徑與規格即可，不為文件或格式改動新增機械測試
- **套用 migration 一律使用 `cd backend && uv run alembic upgrade head`，不可用 MCP 直接執行 SQL**
- 後端架構詳見 `backend/ARCHITECTURE.md`（runtime 架構/資料流/放貸演算法）；測試慣例、SQLAlchemy/Alembic 工作流詳見 `backend/AGENTS.md`（待建；目前散見各 module 註解）
- 前端測試指令、架構慣例詳見 `frontend/AGENTS.md`

### Commit 訊息格式

使用 [Conventional Commits](https://www.conventionalcommits.org/)：`<type>(<scope>)?: <subject>`

- type：`feat` `fix` `docs` `style` `refactor` `perf` `test` `build` `ci` `chore` `revert`
- subject：小寫開頭、祈使句、結尾不加句號
- 由 `lefthook.yml` 的 `commit-msg` hook 強制檢查
