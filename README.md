# BFX Funding Bot

Bitfinex 自動放貸 SaaS 平台 — 透過複合信號決策框架，自動化管理 Bitfinex 融資放貸，最大化收益。

## 技術棧

| 層級 | 技術 |
|------|------|
| Frontend | Next.js 16 (App Router) + Tailwind CSS v4 + shadcn/ui |
| Backend | Python 3.13（長駐 daemon，asyncio + SQLAlchemy 2.0 async + httpx + Alembic） |
| Database | PostgreSQL (Neon) |
| Cache | Redis (Upstash) |
| Auth | JWT (RS256) + HttpOnly Cookie |
| i18n | next-intl (en + zh-TW) |
| Deploy | Vercel (Frontend) + Koyeb (Backend) |

## 專案結構

```
bfx-funding-bot/
├── frontend/                  # Next.js 前端
├── backend_py/                # Python 後端（放貸 daemon）— active
│   └── ARCHITECTURE.md        # 現行 runtime 架構（執行/對帳/放貸演算法）
├── frontend_architecture.md   # 前端架構設計文件
├── backend_architecture.md    # SaaS 平台層設計（產品願景；Go-era 語法僅歷史參考）
└── strategy_specification.md  # 策略設計規範
```

> Go `backend/` 已於 Phase 0 重寫至 `backend_py/` 後封存並移除（2026-05-29），歷史見 git。

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

- [後端 runtime 架構（現行）](backend_py/ARCHITECTURE.md)
- [前端架構設計文件](frontend_architecture.md)
- [SaaS 平台層設計（產品願景）](backend_architecture.md)
- [策略設計規範](strategy_specification.md)
