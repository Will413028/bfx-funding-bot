# BFX Funding Bot

Bitfinex 自動放貸 SaaS 平台 — 透過複合信號決策框架，自動化管理 Bitfinex 融資放貸，最大化收益。

## 技術棧

| 層級 | 技術 |
|------|------|
| Frontend | Next.js 16 (App Router) + Tailwind CSS v4 + shadcn/ui |
| Backend | Go 1.24+ (Gin/Echo) |
| Database | PostgreSQL (Neon) |
| Cache | Redis (Upstash) |
| Auth | JWT (RS256) + HttpOnly Cookie |
| i18n | next-intl (en + zh-TW) |
| Deploy | Vercel (Frontend) + Koyeb (Backend) |

## 專案結構

```
bfx-funding-bot/
├── frontend/                # Next.js 前端
├── backend/                 # Go 後端
├── frontend_architecture.md # 前端架構設計文件
├── backend_architecture.md  # 後端架構設計文件
└── strategy_specification.md # 策略設計規範
```

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
go run ./cmd/server/
```

## 架構文件

- [前端架構設計文件](frontend_architecture.md)
- [後端架構設計文件](backend_architecture.md)
- [策略設計規範](strategy_specification.md)
