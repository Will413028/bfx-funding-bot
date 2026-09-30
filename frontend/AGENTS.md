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
  app/[locale]/                     # i18n 路由；(marketing) 公開、(auth) 登入與雙因素、(dashboard) 受保護
  app/api/auth/[...all]/route.ts    # Better Auth（JWT HTTP endpoints blocked）
  app/api/proxy/[...path]/route.ts  # BFF → Python web-API
  components/ui/                    # shadcn/ui（自動產生，不要手改樣式邏輯）
  components/{layout,shared}/
  features/<feature>/{components,hooks}/  # 一個功能一個模組；現有模組以目錄為準
  lib/                              # api-client、query-keys、env（Zod）、validations、format
messages/                           # i18n JSON（en、zh-TW）
e2e/                                # Playwright
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

變數清單與驗證在 `lib/env.ts`（Zod schema），範本見 `.env.example`。`NEXT_PUBLIC_` 前綴的會進瀏覽器，secret 不可加此前綴。
`BFX_OPERATOR_USER_ID` 前後端必須完全一致；Release 0 preflight 會拒絕缺值、非 admin role、或前後端 operator ID 不一致。

## 監控

- **Sentry**: Client + Server error tracking（optional）

## 部署

見根目錄 `AGENTS.md`「部署架構」與 `docs/runbooks/deploy.md`。
