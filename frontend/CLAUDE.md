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
      (auth)/                       # 登入/雙因素驗證
        actions.ts                  # Server Actions（login, logout）
      (dashboard)/                  # 受保護的 dashboard
        overview/, api-keys/, strategy/, history/, settings/
    api/auth/[...all]/route.ts      # Better Auth（JWT HTTP endpoints blocked）
    api/proxy/[...path]/route.ts    # BFF → Python web-API
  components/
    ui/                             # shadcn/ui（自動產生，不要手改樣式邏輯）
    layout/                         # Sidebar, Topbar, LocaleSwitcher
    shared/                         # 共用元件
  features/                         # 依功能分模組
    auth/components/                # LoginForm, TwoFactorForm
    api-keys/{components,hooks}/    # ApiKeyCard, useApiKeys
    dashboard/{components,hooks}/   # StatsGrid, OffersTable, Charts, useDashboard
    history/{components,hooks}/     # ExecutionTable, BillingTable
    strategy/{components,hooks}/    # StrategyForm, useConfig
    settings/{components,hooks}/    # ChangePasswordForm, useUser
  lib/
    api-client.ts                   # HTTP client（自動解包 { data: T }）
    query-keys.ts                   # TanStack Query key factory
    env.ts                          # Zod 環境變數驗證
    validations.ts                  # 表單 schema（Zod）
    format.ts                       # 格式化工具（APR, USD, period）
  providers/query-provider.tsx      # TanStack Query + Devtools
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

// Client 端 → /api/proxy → Python web-API（注入 server-minted JWT）
// Server 端 → 直接呼叫 API_URL
```

- 錯誤拋出 `ApiError`（含 `status`, `code`, `message`）
- Proxy route 只對 exact public GET allowlist anonymous；其他路徑需要 operator
  identity + fresh session + server-owned MFA marker，再由 server mint JWT。
- Proxy canonicalizes every path and rejects empty/dot/traversal segments before
  constructing the backend URL。

### 認證

- Better Auth（自托）處理 login/logout/TOTP（Server Actions + auth API）；
  self-service signup 永久關閉
- Session 存在 HttpOnly opaque cookie（Better Auth，secondaryStorage → VM Redis，7 天）
- Middleware 用 `getSessionCookie` 檢查 session，保護 dashboard 路由
- Proxy 對後端請求 server-mint 短效 EdDSA JWT（pinned `iss=bfx-funding-bot` / `aud=bfx-funding-backend`，解耦部署 URL；後端用 JWKS 驗證）；JWT 不會進 browser response
- 未完成 TOTP 的 pending session 不得取得 execution JWT；僅 exact per-session MFA marker 可放行
- 只有 `/login`、`/two-factor` auth pages；dashboard middleware 只用 session cookie 做路由級 guard

### 狀態管理

| 類型 | 工具 | 用途 |
|------|------|------|
| Server state | TanStack Query v5 | API 資料快取（staleTime 60s） |
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
NEXT_PUBLIC_APP_URL          # 前端 URL
NEXT_PUBLIC_APP_NAME         # App 名稱
NEXT_PUBLIC_BETTER_AUTH_URL  # Better Auth base URL（client SDK 用）
NEXT_PUBLIC_SENTRY_DSN       # Sentry（optional）
NEXT_PUBLIC_AXIOM_DATASET    # Axiom（optional）

# Server-only
API_URL                    # Python web-API URL（proxy + server actions 用）
BETTER_AUTH_URL            # Better Auth base URL（server，baseURL / trustedOrigins / passkey origin）
BETTER_AUTH_SECRET         # Better Auth 加密金鑰（≥ 32 chars）
DATABASE_URL               # VM-local Postgres（Better Auth `auth` schema，bfx_webauth，direct 無 -pooler）
REDIS_URL                  # VM-local Redis（session / rate-limit secondaryStorage，ioredis）
PASSKEY_RP_ID              # Passkey relying-party ID（domain）
BFX_OPERATOR_USER_ID       # Better Auth user ID；前後端必須完全一致
BFX_OPERATOR_ROLE          # Release 0 固定為 admin
```

驗證邏輯在 `lib/env.ts`（Zod schema）。

## 監控

- **Sentry**: Client + Server error tracking（optional）
- **Axiom**: 日誌（via next-axiom，optional）

## 部署

- 平台：Oracle Cloud VM（Docker Compose；Frontend、Python web-API、Postgres、Redis）
- Config：`docker-compose.bot.yml`、`scripts/deploy-vm.sh`
- Release 0 preflight 會拒絕缺值、非 admin role、或 frontend/backend operator ID 不一致
- React Compiler 已啟用（自動 memoization）
