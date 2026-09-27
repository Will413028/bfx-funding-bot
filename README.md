# BFX Funding Bot

Bitfinex 自動放貸 SaaS 平台，自動化管理融資放貸、資金配置與對帳。

## 技術棧

| 層級 | 技術 |
|------|------|
| Frontend | Next.js 16 (App Router) + Tailwind CSS v4 + shadcn/ui |
| Backend | Python 3.13（長駐 daemon，asyncio + SQLAlchemy 2.0 async + httpx + Alembic） |
| Database | PostgreSQL（Oracle VM，Better Auth `auth` schema + bot data） |
| Cache | Redis（Oracle VM，session/rate-limit secondary storage） |
| Auth | Better Auth EdDSA JWT (server-to-server only) + HttpOnly Cookie |
| i18n | next-intl (en + zh-TW) |
| Deploy | Oracle Cloud VM（Docker Compose：Frontend + Python web-API + PostgreSQL + Redis） |

## 專案結構

```
bfx-funding-bot/
├── frontend/                # Next.js 前端
│   └── AGENTS.md            # 現行前端架構慣例與開發指令
├── backend/                 # Python 後端（放貸 daemon）
│   └── ARCHITECTURE.md      # 現行 runtime 架構（執行/對帳/放貸演算法）
└── docs/
    └── runbooks/           # 部署、維運與災難復原操作
```

> Go 後端與 Go-era `backend_architecture.md`（SaaS 平台層藍本）已於 2026-05-29 移除（重寫為 Python 後成死碼；Python 版原名 `backend_py/`，2026-09-23 改名為 `backend/`）；歷史見 git。

## 開發

### Frontend

```bash
cd frontend
pnpm install
pnpm dev
```

### Backend

```bash
cd backend
uv run alembic upgrade head        # 套用 DB migration
uv run pytest -m "not integration" # 單元測試
uv run python -m bfx_funding_bot.apps.bot  # 跑 daemon（phase 由 BFX_PHASE 控制）
```

## 架構文件

- [後端 runtime 架構與放貸演算法](backend/ARCHITECTURE.md)
- [前端架構慣例與開發指令](frontend/AGENTS.md)
- [Deploy runbook](docs/runbooks/deploy.md) — CI 建 image 推 GHCR，VM 上 `bfx-deploy`
  以 digest 部署、migration 前備份與還原測試、失敗回滾與 `deployments` ledger。
- [Operations runbook](docs/runbooks/operations.md) — 幣別啟停、放貸包絡、交易狀態、UI（TOTP）
  恢復／kill、自動保護、UNKNOWN 處理與 Telegram 告警。
- [Fresh host setup](docs/runbooks/fresh-host-setup.md) — 新主機的 DB roles、公開入口與首次安裝工具。
- [Release 0 operator-only containment runbook](docs/runbooks/release-0-operator-containment.md)
- [Research one-shot jobs](docs/runbooks/research-one-shot-jobs.md) — 研究資料補齊與一次性容器。
- [Rollback after a venue write](docs/runbooks/rollback-after-venue-write.md) — Venue 寫入後的回滾與還原判斷。
- [Projection archive 契約](docs/runbooks/projection-audit-cutover.md) — Archive 驗證、restore 與 atomic apply 邊界。
- [Offsite DR Terraform module](infra/terraform/r2/README.md) and
  [operator runbook](docs/runbooks/offsite-dr.md): Terraform manages only R2
  infrastructure; the VM wizard manages runtime secret injection.
