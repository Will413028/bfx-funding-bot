# bfx-funding-bot

Bitfinex 自動放貸 SaaS 平台。

## 專案結構

- `backend/` — Python 3.13 後端（FastAPI + SQLAlchemy 2.0 async + httpx + Alembic）— **active**
- `frontend/` — Next.js 16 前端
- ~~Go 1.25 後端~~ — **已移除**（Phase 0 重寫為 Python 後封存並刪除，2026-05-29；歷史見 git）。Python 後端原名 `backend_py/`，2026-09-23 改名為 `backend/`，舊文件與 SDD 裡的 `backend_py` 指的就是現在的 `backend/`
- `backend/ARCHITECTURE.md` — **後端架構 source of truth**（event-sourced execution、reconcile 骨幹、deployment reconciler、放貸演算法、event model、DB schema、safety、phases/部署、key invariants；含 mermaid 架構/資料流圖）
- 維運 runbook 在 `docs/runbooks/`；設計決策紀錄（ADR）、研究報告與 roadmap 另行私下保存，不放在本 repo（`.gitignore` 擋住舊路徑）

## 部署架構

應用程式與資料服務自托於 Oracle Cloud VM（單機 Docker）；外部服務包含 Bitfinex venue、GHCR（image registry）與 Cloudflare R2 offsite backup。

- **應用程式**（bot／webapi／frontend）：compose project `bfx-app`，定義在 `deploy/vm/docker-compose.app.yml`。CI（`.github/workflows/release.yml`）在綠燈的 `main` commit 建 arm64 image 推到 GHCR；VM 的 `bfx-deploy`（`deploy/vm/ops/bfx_deploy.py`，`bfx-deploy.timer` 每 5 分鐘）只以 digest 部署（部署不改變交易狀態、沒有分級或核准）：有 migration 時先停 bot 再備份與跑 isolated restore test（用目標版本的 DR 腳本）、寫 `deployments` ledger 的 `started` 列後 recreate、健康檢查、無 migration 時失敗回滾，結束再寫一列並發 Telegram；成功後安裝該版本的主機工具（下一輪生效）。container hardening 由 CI 對 `docker compose config` 做 policy 檢查。VM 不 build image。
- **資料服務**（Postgres／Redis）：compose project `bfx`，`docker-compose.bot.yml`；其 `legacy-app` profile 只是歷史定義，不可用來啟動應用程式。
- Runbook：部署 `docs/runbooks/deploy.md`；交易狀態／包絡／停機／告警 `docs/runbooks/operations.md`；新主機的 DB roles／公開入口／首次安裝工具 `docs/runbooks/fresh-host-setup.md`；研究用一次性容器 `docs/runbooks/research-one-shot-jobs.md`。

| 服務 | 平台 |
|------|------|
| Frontend | VM `bfx-frontend`（Next.js standalone，公開入口 443→`127.0.0.1:3001`，目前是 Tailscale Funnel；實際公開 host 見 `AGENTS.local.md`，設定見 `docs/runbooks/fresh-host-setup.md`） |
| Backend (bot + webapi) | VM（`bfx-bot` / `bfx-webapi`；webapi 內網限定，FE 經 docker 網路呼叫） |
| Database | 自托 Postgres 18（`bfx-postgres`，volume `bfx_pgdata`；roles：bot owner、`bfx_webapi`、`bfx_webauth`） |
| Cache | 自托 Redis 7（`bfx-redis`，volume `bfx_redisdata`；Better Auth secondaryStorage：session + rate-limit，ioredis） |

- 備份／WAL archive／isolated restore 依 `docs/runbooks/offsite-dr.md`；pgBackRest backup/status、`bfx-backup-check`、每月 `bfx-restore-test`（prefix-hash 驗證）timer 定義在 `deploy/vm/systemd/`（第一次由 `deploy/vm/ops/install.sh`、之後由 bfx-deploy 依 `managed-units` 安裝，都不啟用），實際啟用與健康狀態須查目標環境。
- 每週 attribution／G3 報告由 `bfx-weekly-report.timer` 觸發 `bfx-weekly-report.service`，執行主機工具裡的 `deploy/vm/ops/bfx_weekly_report.py`（步驟在 `deploy/vm/ops/docker-compose.weekly-report.yml`，隨每次部署安裝；image 用 ledger 的 backend digest；不讀 VM checkout）；操作前核對目前 unit、排程及輸出（`docs/runbooks/operations.md` §8）。Redis session 為 ephemeral。

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
- 純文件修改（runbook、ARCHITECTURE.md 等）不需要走完整工作流

### 營運原則：放貸全自動

- 目標是全自動放貸：掛單、續借、halt 後恢復、幣種切換（cutover）與 canary 都由系統完成。設計、計畫與 runbook 不得把手動 Bitfinex 掛單、手動 Kill／TOTP 之類的人工步驟當成正常流程的一環。
- 做不到自動化時，說明卡在哪裡，並提出自動化的替代方案；不要改成請 operator 手動補上。operator kill（cancel-all）只是緊急控制，不屬於正常流程。
- watcher／monitor 的結束條件要先對照真實狀態驗證再依賴，例如剛 push 完 CI job 可能還不存在，這時「沒有執行中的 job」不代表已完成。

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
