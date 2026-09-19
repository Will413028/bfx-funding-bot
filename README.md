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
├── backend_py/                # Python 後端（放貸 daemon）— active
│   └── ARCHITECTURE.md        # 現行 runtime 架構（執行/對帳/放貸演算法）
├── frontend_architecture.md   # 前端架構設計文件
└── strategy_specification.md  # 策略設計規範
```

> Go `backend/` 與 Go-era `backend_architecture.md`（SaaS 平台層藍本）已於 2026-05-29 移除（重寫至 `backend_py/` 後成死碼）；歷史見 git，Phase 5+ SaaS 方向見 `ROADMAP.md`。

## 開發

### Frontend

```bash
cd frontend
pnpm install
pnpm dev
```

### Backend

```bash
cd backend_py
uv run alembic upgrade head        # 套用 DB migration
uv run pytest -m "not integration" # 單元測試
uv run bfx-shadow                  # 跑 daemon（phase 由 BFX_PHASE 控制）
```

## 架構文件

- [Immutable capital-policy release 與人工啟用流程](docs/runbooks/immutable-release.md)
  — build once、明確 schema/policy apply、保留既有 PG/Redis 與 durable halt；
  不使用舊 moving-main/phase deploy，technical startup 不等於放貸啟用。
- [後端 runtime 架構（現行）](backend_py/ARCHITECTURE.md)
- [前端架構設計文件](frontend_architecture.md)
- [策略設計規範](strategy_specification.md)
- [Release 0 operator-only containment runbook](docs/runbooks/release-0-operator-containment.md)
- [Offsite DR Terraform module](infra/terraform/r2/README.md) and
  [operator runbook](docs/runbooks/offsite-dr.md): Terraform manages only R2
  infrastructure; the VM wizard manages runtime secret injection.
