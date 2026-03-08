## Context

後端 API 48 項全部完成，前端尚未建立。本 change 建立 `frontend/` 專案骨架，為後續 F2-F15 的頁面開發奠定基礎。架構設計已記錄在 `frontend_architecture.md`，本 design 聚焦於 scaffold 階段的技術決策。

## Goals / Non-Goals

**Goals:**
- 建立可立即開發頁面的 Next.js 16 專案（`pnpm dev` 能啟動）
- 設定完整的工具鏈（Tailwind v4, shadcn/ui, Biome, Knip）
- 建立 next-intl 多語系路由架構（en + zh-TW）
- 安裝並設定所有 Phase 1 狀態管理工具（Zustand, TanStack Query, nuqs, RHF + Zod）
- 建立三個路由群組骨架（marketing, auth, dashboard）各含 placeholder 頁面
- 建立暗色金融風格主題

**Non-Goals:**
- 實作任何業務頁面（F4-F11 範圍）
- 實作 api-client.ts、format.ts 等核心工具（F2 範圍）
- 實作同源代理 proxy route（F3 範圍）
- 實作 WebSocket 相關功能（F12-F13 範圍）
- 設定 Sentry 或 CSP 安全標頭（F15 範圍）

## Decisions

### 1. 套件管理器：pnpm
- **選擇**：pnpm（非 npm/yarn/bun）
- **理由**：磁碟效率最佳（hard link）、嚴格的 node_modules 結構防止幽靈依賴、Vercel 原生支援
- **替代方案**：bun 速度更快但 Next.js 16 的 Server Actions 相容性尚未完全穩定

### 2. shadcn/ui 初始化：手動設定（非 CLI init）
- **選擇**：手動建立 `components.json` + `globals.css`，不跑 `npx shadcn@latest init`
- **理由**：CLI init 會覆寫 globals.css 為預設淺色主題，我們需要自訂暗色金融風格。手動設定更可控
- **做法**：先建立 `components.json` 指定路徑和樣式，後續用 `npx shadcn@latest add button` 逐個加入元件

### 3. Locale Layout 包含所有 Providers
- **選擇**：QueryProvider + NuqsAdapter + NextIntlClientProvider 全部放在 `[locale]/layout.tsx`
- **理由**：避免多層巢狀 provider 檔案、locale layout 是所有頁面的共同祖先、Phase 1 就需要這些工具
- **Provider 順序**：NextIntlClientProvider > QueryProvider > NuqsAdapter > children

### 4. 路由群組：placeholder 頁面
- **選擇**：每個路由群組建立一個最小 placeholder 頁面（僅顯示標題）
- **理由**：確保路由結構正確、layout 正常運作、middleware 權限檢查可驗證
- **範圍**：`(marketing)/page.tsx`、`(auth)/login/page.tsx`、`(dashboard)/overview/page.tsx`

### 5. Biome 取代 ESLint + Prettier
- **選擇**：僅用 Biome，不安裝 ESLint
- **理由**：Biome 整合 formatter + linter，速度比 ESLint + Prettier 快 10x+，設定更簡潔
- **注意**：需移除 Next.js 預設的 eslint 設定

## Risks / Trade-offs

- **[Next.js 16 尚新]** → 遵循官方文件 + next-intl 官方 App Router 範例，避免使用實驗性 API
- **[shadcn/ui 手動設定]** → 需確保 `components.json` 的 aliases 和路徑與實際目錄一致，否則 `npx shadcn add` 會失敗
- **[Tailwind v4 breaking changes]** → v4 改用 CSS-first 設定（`@theme` 取代 `tailwind.config.js`），需確認 shadcn/ui 元件相容性
