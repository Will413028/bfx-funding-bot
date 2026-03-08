## Why

後端 API 全部就緒（48 項完成），需要建立前端專案基礎架構，才能開始開發使用者介面。選擇 Next.js 16 App Router 搭配 next-intl 多語系，從第一天就建立正確的目錄結構和工具鏈，避免日後遷移成本。

## What Changes

- 建立 `frontend/` 目錄，初始化 Next.js 16 專案（App Router + Turbopack）
- 設定 Tailwind CSS v4 + shadcn/ui 元件庫（暗色金融風格主題）
- 設定 next-intl 多語系架構（en + zh-TW），包含 `[locale]` 路由、middleware、翻譯檔
- 安裝並設定狀態管理工具：Zustand、TanStack Query v5、nuqs
- 安裝表單工具：React Hook Form + Zod v4
- 設定程式碼品質工具：Biome（formatter + linter）、Knip（unused code detection）
- 建立 root layout、locale layout（含所有 providers）、global error boundary
- 建立三個路由群組骨架：`(marketing)/`、`(auth)/`、`(dashboard)/`

## Capabilities

### New Capabilities
- `project-setup`: Next.js 16 專案初始化 + 套件安裝 + 設定檔（next.config.ts, postcss, biome.json, knip.config.ts, components.json, .env.example）
- `i18n-routing`: next-intl 多語系路由架構（routing.ts, request.ts, navigation.ts, middleware.ts）+ 翻譯檔骨架
- `layout-providers`: Root layout + locale layout（NextIntlClientProvider, QueryProvider, NuqsAdapter）+ 路由群組骨架 + global error boundary
- `theme-styling`: Tailwind v4 + shadcn/ui 暗色金融主題（globals.css 色彩變數、PostCSS 設定、cn() 工具函式）

### Modified Capabilities

（無，這是全新的前端專案）

## Impact

- 新增 `frontend/` 目錄（不影響現有 `backend/` 程式碼）
- 新增 `frontend/package.json` 依賴：next, react, tailwindcss, @tanstack/react-query, zustand, next-intl, nuqs, react-hook-form, zod, biome, knip 等
- 部署平台：Vercel（透過 Git 整合自動部署）
- 開發環境需求：Node.js 22+, pnpm
