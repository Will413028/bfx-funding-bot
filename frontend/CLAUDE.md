# Frontend — Next.js 16 (App Router)

## 快速指令

```bash
pnpm dev          # 開發（Turbopack HMR）
pnpm build        # Production build
pnpm lint         # tsc + biome check
pnpm format       # biome format --write
pnpm knip         # 找未使用的 exports
pnpm test         # Vitest 單次執行
pnpm test:watch   # Vitest watch mode
pnpm test:e2e     # Playwright E2E
```

## 目錄結構

```
src/
  app/
    layout.tsx                      # Root layout（metadata）
    [locale]/                       # i18n 路由區段
      layout.tsx                    # Locale + providers
      (marketing)/                  # 公開頁面（landing, pricing）
      (auth)/                       # 登入/註冊
        actions.ts                  # Server Actions（login, register, logout）
      (dashboard)/                  # 受保護的 dashboard
        overview/, api-keys/, strategy/, history/, settings/
    api/proxy/[...path]/route.ts    # Auth proxy → Go backend
  components/
    ui/                             # shadcn/ui（自動產生，不要手改樣式邏輯）
    layout/                         # Sidebar, Topbar, LocaleSwitcher
    shared/                         # 共用元件
  features/                         # 依功能分模組
    auth/components/                # LoginForm, RegisterForm
    api-keys/{components,hooks}/    # ApiKeyCard, useApiKeys
    dashboard/{components,hooks}/   # StatsGrid, OffersTable, Charts, useDashboard
    history/{components,hooks}/     # ExecutionTable, BillingTable
    strategy/{components,hooks}/    # StrategyForm, useConfig
    settings/{components,hooks}/    # ChangePasswordForm, useUser
  lib/
    api-client.ts                   # HTTP client（自動解包 { data: T }）
    ws-client.ts                    # WebSocket（指數退避重連）
    query-keys.ts                   # TanStack Query key factory
    env.ts                          # Zod 環境變數驗證
    validations.ts                  # 表單 schema（Zod）
    format.ts                       # 格式化工具（APR, USD, period）
  providers/query-provider.tsx      # TanStack Query + Devtools
  stores/ws-store.ts                # Zustand（WebSocket 狀態）
  types/index.ts                    # 所有 TypeScript 型別
  i18n/                             # next-intl 設定
messages/                           # i18n JSON（en.json, zh-TW.json）
e2e/                                # Playwright 測試
middleware.ts                       # Auth guard + i18n + security headers
```

## 架構慣例

### API 通訊

```typescript
// api-client 自動解包 { data: T } envelope
const user = await apiClient.get<User>("/users/me");
const keys = await apiClient.getList<ApiKey[]>("/api-keys");

// Client 端 → /api/proxy → Go backend（注入 auth cookie）
// Server 端 → 直接呼叫 API_URL
```

- 錯誤拋出 `ApiError`（含 `status`, `code`, `message`）
- Proxy route 驗證路徑前綴 `/api/v1/`，防止 path traversal

### 認證

- Server Actions 處理 login/register/logout
- Token 存在 HttpOnly cookie（`auth_token`，7 天）
- Middleware 檢查 JWT 過期，保護 dashboard 路由
- 已登入用戶自動跳過 login/register 頁面

### 狀態管理

| 類型 | 工具 | 用途 |
|------|------|------|
| Server state | TanStack Query v5 | API 資料快取（staleTime 60s） |
| Client state | Zustand | WebSocket 連線狀態 |
| URL state | nuqs | Query string 參數 |
| Form state | react-hook-form + Zod | 表單驗證 |

### Feature 模組模式

每個 feature 擁有自己的 `components/` 和 `hooks/`：

```typescript
// hooks 封裝 TanStack Query
export function useApiKeys() {
  return useQuery({
    queryKey: apiKeyKeys.list(),
    queryFn: () => apiClient.get<ApiKey[]>("/api-keys"),
  });
}
```

- Query keys 集中管理在 `lib/query-keys.ts`
- Mutation 成功後用 `invalidateQueries` 刷新快取

### i18n

- 支援：en, zh-TW（預設 en）
- 路由：`/{locale}/path`
- 使用 `next-intl` 的 `Link`、`useRouter`（自動加 locale prefix）
- 表單錯誤訊息用 i18n key，搭配 `useTranslations("validation")`

## 樣式

- **Tailwind CSS 4** + CSS variables（預設 dark theme）
- **shadcn/ui**（Radix UI）：元件在 `components/ui/`
- `cn()` 工具函式合併 class（`lib/utils.ts`）
- **Biome** 取代 ESLint/Prettier

## 環境變數

```bash
# Public（瀏覽器可見，需 NEXT_PUBLIC_ 前綴）
NEXT_PUBLIC_APP_URL        # 前端 URL
NEXT_PUBLIC_APP_NAME       # App 名稱
NEXT_PUBLIC_WS_URL         # WebSocket（client 直連 backend）
NEXT_PUBLIC_SENTRY_DSN     # Sentry（optional）
NEXT_PUBLIC_AXIOM_DATASET  # Axiom（optional）

# Server-only
API_URL                    # Go backend URL（proxy + server actions 用）
AUTH_SECRET                # Middleware JWT 驗證
```

驗證邏輯在 `lib/env.ts`（Zod schema）。

## 監控

- **Sentry**: Client + Server error tracking（optional）
- **Axiom**: 日誌（via next-axiom，optional）

## 部署

- 平台：Vercel
- Config：`vercel.json`（framework: nextjs）
- React Compiler 已啟用（自動 memoization）
