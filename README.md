# BFX Funding Bot

Bitfinex 自動放貸 SaaS 平台 — 透過複合信號決策框架，自動化管理 Bitfinex 融資放貸，最大化收益。

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
├── frontend/                  # Next.js 前端
├── backend/                # Python 後端（放貸 daemon）— active
│   └── ARCHITECTURE.md        # 現行 runtime 架構（執行/對帳/放貸演算法）
├── frontend_architecture.md   # 前端架構設計文件
└── strategy_specification.md  # 策略設計規範
```

> Go 後端與 Go-era `backend_architecture.md`（SaaS 平台層藍本）已於 2026-05-29 移除（重寫為 Python 後成死碼；Python 版原名 `backend_py/`，2026-09-23 改名為 `backend/`）；歷史見 git，Phase 5+ SaaS 方向見 `ROADMAP.md`。

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
uv run bfx-shadow                  # 跑 daemon（phase 由 BFX_PHASE 控制）
```

## 架構文件

- [Deploy runbook](docs/runbooks/deploy.md) — CI 建 image 推 GHCR，VM 上 `bfx-deploy`
  以 digest 部署、變更分級、備份後 migrate、失敗回滾與 `deployments` ledger。
- [Operations runbook](docs/runbooks/operations.md) — 交易狀態、UI（TOTP）核准／恢復／暫停／
  kill、限額期、自動保護、UNKNOWN／orphan 處理、Telegram 告警。
- [後端 runtime 架構（現行）](backend/ARCHITECTURE.md)
- [前端架構設計文件](frontend_architecture.md)
- [策略設計規範](strategy_specification.md)
- [Release 0 operator-only containment runbook](docs/runbooks/release-0-operator-containment.md)
- [Offsite DR Terraform module](infra/terraform/r2/README.md) and
  [operator runbook](docs/runbooks/offsite-dr.md): Terraform manages only R2
  infrastructure; the VM wizard manages runtime secret injection.
