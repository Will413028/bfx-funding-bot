# Frontend — Next.js 16 (App Router)

## 快速指令

```bash
pnpm dev          # 開發（Turbopack HMR）
pnpm build        # Static export → out/（由 web API 提供）
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
  app/[locale]/                     # i18n 路由（generateStaticParams 預先產生 en、zh-TW）；(dashboard) 營運頁
  app/not-found.tsx                 # 找不到的路徑（export 成 404.html）
  components/ui/                    # shadcn/ui（自動產生，不要手改樣式邏輯）
  components/{layout,shared}/
  features/<feature>/{components,hooks}/  # 一個功能一個模組；現有模組以目錄為準
  lib/                              # api-client、query-keys、validations、format、sentry
messages/                           # i18n JSON（en、zh-TW）
e2e/                                # Playwright
instrumentation-client.ts           # 瀏覽器的 Sentry init（tunnel /fe-events）
```

## 架構慣例

### API 通訊

```typescript
// api-client 自動解包 { data: T } envelope
const user = await apiClient.get<User>("/users/me");
const keys = await apiClient.getList<ApiKey[]>("/api-keys");

// 只在瀏覽器呼叫，同源：/api/v1（api-client 的 API_BASE）→ 提供這個 UI 的 web API
```

- 錯誤拋出 `ApiError`（含 `status`, `code`, `message`）
- 寫入請求一律 JSON；web API 拒絕跨站、非同源（`Origin`）或非 JSON 的寫入。

### Static export 與身分（9d-5）

- `next.config.ts` 是 `output: "export"`：沒有 Next server、middleware、route handler、server action、
  `next/headers`、rewrite；`src/lib/__tests__/dashboard-client-only.test.ts` 守住這件事。
- 身分不在 UI：web API 只經 Tailscale Serve 進入，Serve 轉送 tailnet policy 授予的 operator capability，
  web API 據此授權。UI 沒有登入、登出或 TOTP。
- web API 回應帶 hash 型 CSP（export 裡每段 inline script 的 sha256）；不要加需要 `unsafe-inline` 或
  runtime nonce 的寫法。

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

只有 build 時的 `NEXT_PUBLIC_SENTRY_DSN`（選填，範本 `.env.example`）。`NEXT_PUBLIC_` 前綴的會進瀏覽器，secret 不可放進前端。

## 監控

- **Sentry**: 只有瀏覽器（`instrumentation-client.ts`）；DSN 由 build arg `NEXT_PUBLIC_SENTRY_DSN` 注入，未設即關閉；經 `src/lib/sentry.ts` 的 `sentryOptions`（遮罩 email／IP、不送 PII）；事件送到同源的 `/fe-events`（`src/lib/sentry-tunnel.ts`），由 web API 只轉給 `BFX_FE_SENTRY_DSN` 的 project。見 `docs/runbooks/operations.md`「Error tracking」。

## 部署

沒有前端 image：`backend/Dockerfile` 的 `ui` stage 把這個目錄 build 成 static export 放進 backend image，由 web API 提供。見根目錄 `AGENTS.md`「部署架構」與 `docs/runbooks/deploy.md`。
