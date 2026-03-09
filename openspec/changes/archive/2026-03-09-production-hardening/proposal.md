## Why

前端即將部署至 Vercel，需要補齊生產環境必備的基礎設施：錯誤監控（Sentry）、安全標頭（CSP）、部署設定（vercel.json），以及確保 DevTools 不洩漏至正式環境。目前這些都缺失或只有註解佔位。

## What Changes

- 整合 `@sentry/nextjs` 錯誤監控（啟用 next.config.ts 中已註解的 Sentry 設定）
- 在 Next.js middleware 加入 CSP + 常見安全標頭（X-Frame-Options, X-Content-Type-Options 等）
- 建立 `vercel.json` 部署設定（headers、rewrites）
- ReactQueryDevtools 改為僅在 `NODE_ENV === "development"` 時載入
- env.ts 加入啟動時 fail-fast 行為（server 端 parse 失敗直接 throw，不靜默降級）

## Capabilities

### New Capabilities

- `frontend-sentry`: Sentry SDK 整合（next.config.ts + sentry.*.config.ts + error boundary）
- `frontend-security-headers`: CSP + 安全標頭（middleware 層注入）
- `frontend-deploy-config`: Vercel 部署設定（vercel.json + 環境變數規劃）

### Modified Capabilities

_(無既有 spec 需變更)_

## Impact

- **Dependencies**: 新增 `@sentry/nextjs`
- **Config**: 新增 `sentry.client.config.ts`, `sentry.server.config.ts`, `sentry.edge.config.ts`, `vercel.json`
- **Middleware**: `middleware.ts` 擴充安全標頭邏輯
- **Providers**: `query-provider.tsx` 條件載入 DevTools
- **Env**: `env.ts` 強化驗證行為
- **next.config.ts**: 啟用 `withSentryConfig` 包裝
