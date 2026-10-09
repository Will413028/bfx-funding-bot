# bfx-funding-bot

Bitfinex 自動放貸 SaaS 平台。

## 專案結構

- `backend/` — Python 3.13 後端（FastAPI + SQLAlchemy 2.0 async + httpx + Alembic）— **active**
- `frontend/` — Next.js 16 前端
- Python 後端曾名 `backend_py/`；舊文件與 SDD 裡的 `backend_py` 指的就是現在的 `backend/`
- `backend/ARCHITECTURE.md` — **後端架構 source of truth**（ledger 資金紀錄：insert-once journal＋已接受的 venue 觀測、command gate、observation cycle 與 reconcile 骨幹、UNKNOWN 結案與 quarantine、deployment reconciler、放貸演算法、DB schema、safety、phases/部署、key invariants；含 mermaid 架構/資料流圖）
- 維運 runbook 在 `docs/runbooks/`；決策紀錄（ADR）：`docs/adr/`；研究報告與 roadmap 另行私下保存，不放在本 repo（`.gitignore` 擋住舊路徑）

## 部署架構

應用程式與資料服務自托於 Oracle Cloud VM（單機 Docker）；外部服務包含 Bitfinex venue、GHCR（image registry）與 Cloudflare R2 offsite backup。

- **應用程式**（bot／webapi／frontend）：compose project `bfx-app`（`deploy/vm/docker-compose.app.yml`）。CI（`.github/workflows/release.yml`）在綠燈的 `main` commit 建 arm64 image 推到 GHCR；VM 的 `bfx-deploy` timer 只以 digest 部署，VM 不 build image，部署不改變交易狀態。一次部署的步驟（migration 前停 bot、備份與 restore test、ledger、回滾、通知）見 `docs/runbooks/deploy.md`。
- **資料服務**（Postgres／Redis）：compose project `bfx`，`docker-compose.bot.yml`（只有 postgres 與 redis）。
- Runbook：部署 `docs/runbooks/deploy.md`；交易狀態／包絡／停機／告警 `docs/runbooks/operations.md`；新主機的 DB roles／公開入口／首次安裝工具 `docs/runbooks/fresh-host-setup.md`；研究用一次性容器 `docs/runbooks/research-one-shot-jobs.md`。

| 服務 | 平台 |
|------|------|
| Frontend | VM `bfx-frontend`（Next.js standalone，公開入口 443→`127.0.0.1:3001`，目前是 Tailscale Funnel；實際公開 host 見 `AGENTS.local.md`，設定見 `docs/runbooks/fresh-host-setup.md`） |
| Backend (bot + webapi) | VM（`bfx-bot` / `bfx-webapi`；webapi 內網限定，FE 經 docker 網路呼叫） |
| Database | 自托 Postgres 18（`bfx-postgres`，volume `bfx_pgdata`；roles：bot owner、`bfx_webapi`、`bfx_webauth`） |
| Cache | 自托 Redis 7（`bfx-redis`，volume `bfx_redisdata`；Better Auth secondaryStorage：session + rate-limit，ioredis） |

- 備份與 DR 見 `docs/runbooks/offsite-dr.md`，定期與背景工作（含每週報告）見 `docs/runbooks/operations.md` §8；systemd unit 在 `deploy/vm/systemd/`，實際啟用與健康狀態須查目標環境。
- Redis session 為 ephemeral。

## 指令執行目錄

不同工具必須在正確的目錄下執行：

| 工具 | 執行目錄 | 範例 |
|------|----------|------|
| `pytest`（單元，序列） | `backend/` | `cd backend && uv run pytest -m "not integration"` |
| `pytest`（單元，平行） | `backend/` | `cd backend && uv run pytest -m "not integration" -n auto` |
| `pytest`（整合，平行） | `backend/` | `cd backend && uv run pytest -m integration -n 6 --dist loadfile`（CI 用 `-n 4`） |
| `mypy` / `ruff` | `backend/` | `cd backend && uv run mypy src/ && uv run ruff check` |
| `alembic revision --autogenerate` | `backend/` | `cd backend && uv run alembic revision --autogenerate -m "..."` |
| `alembic upgrade head` | `backend/` | `cd backend && uv run alembic upgrade head` |
| `alembic check` | `backend/` | `cd backend && uv run alembic check`（驗證 metadata 與 DB 無 drift） |

測試資料庫：DB 測試只用本機 PostgreSQL 18（每個 pytest process／xdist worker 各自啟動一台拋棄式 server，不用 Docker）。需要 PG18 binaries：macOS `brew install postgresql@18`（自動找 `/opt/homebrew/opt/postgresql@18/bin`），其他環境用 `BFX_TEST_PG_BIN` 指向 `bin` 目錄（Linux 為 PGDG `/usr/lib/postgresql/18/bin`）；major 必須等於 `deploy/vm/postgres/Dockerfile` 的 `FROM postgres:<major>`，否則測試開始即失敗，不會退回 Docker 或別的版本。標 `docker` 的測試（真實容器、`docker exec psql`）無 Docker daemon 時自動 skip；`BFX_REQUIRE_DOCKER=1`（CI 設定）改為失敗。預設序列執行，`addopts` 沒有 `-n`。

備註：`backend/` 必須 cd 進去才會走 uv 管的 Python 3.13；從 repo root 直接跑會撞 pyenv 3.12 的 sqlalchemy。`.env` 是 repo root 的 symlink（worktree 重建後要 `ln -sf ../.env backend/.env`）。

## 開發規範

### 開發工作流

需求未決或架構取捨先設計討論；多步驟工作先整理計畫。明確且低風險的修正直接實作與受影響驗證；放貸／風控／DB 行為變更補回歸測試並 review。依任務需要選用可用的 skills，仍須遵守架構與驗證要求。

規則：
- 每個功能必須包含單元測試，`cd backend && uv run pytest -m "not integration"` 全過才能 commit
- 純文件修改（runbook、ARCHITECTURE.md 等）不需要走完整工作流

### 營運原則：放貸全自動

- 目標是全自動放貸：掛單、續借、halt 後恢復、幣種切換（cutover）與 canary 都由系統完成。設計、計畫與 runbook 不得把手動 Bitfinex 掛單、手動 Kill／TOTP 之類的人工步驟當成正常流程的一環。
- 做不到自動化時，說明卡在哪裡，並提出自動化的替代方案；不要改成請 operator 手動補上。operator kill（cancel-all）只是緊急控制，不屬於正常流程。

### 測試與品質（強制）

- **行為變更補對應的回歸測試**；純文件改動核對指令、路徑與規格即可，不為文件或格式改動新增機械測試
- **套用 migration 一律使用 `cd backend && uv run alembic upgrade head`，不可用 MCP 直接執行 SQL**
- 後端架構詳見 `backend/ARCHITECTURE.md`（runtime 架構/資料流/放貸演算法）；測試慣例與 SQLAlchemy/Alembic 工作流以各 module 註解與既有測試為準
- 前端測試指令、架構慣例詳見 `frontend/AGENTS.md`

### Commit 訊息格式

使用 [Conventional Commits](https://www.conventionalcommits.org/)：`<type>(<scope>)?: <subject>`

- type：`feat` `fix` `docs` `style` `refactor` `perf` `test` `build` `ci` `chore` `revert`
- subject：小寫開頭、祈使句、結尾不加句號
- 由 `lefthook.yml` 的 `commit-msg` hook 強制檢查
