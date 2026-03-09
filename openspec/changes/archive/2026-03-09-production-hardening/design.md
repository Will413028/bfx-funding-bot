## Context

前端使用 Next.js 16 + Vercel 部署。目前缺乏：
- 錯誤監控（Sentry 設定已註解在 `next.config.ts`）
- 安全標頭（middleware 僅處理 i18n + auth redirect）
- 部署設定（無 `vercel.json`）
- DevTools 在 production 可見（`ReactQueryDevtools` 無條件渲染）

## Goals / Non-Goals

**Goals:**
- Sentry 整合：捕捉 client/server/edge 層錯誤
- CSP + 安全標頭：防禦 XSS、clickjacking 等常見攻擊
- Vercel 部署設定：headers 配置、proxy rewrite
- Production DevTools 隱藏
- env 驗證 fail-fast

**Non-Goals:**
- Sentry performance monitoring / session replay（後續開啟）
- 自訂 Sentry error boundary UI（使用預設行為）
- Rate limiting（已在 backend middleware 實作）

## Decisions

### 1. Sentry SDK 整合方式
使用 `@sentry/nextjs` wizard 生成的三檔結構：
- `sentry.client.config.ts` — client-side SDK init
- `sentry.server.config.ts` — server-side SDK init
- `sentry.edge.config.ts` — edge runtime SDK init

**Rationale**: 這是 Sentry 官方推薦的 Next.js 整合方式，`withSentryConfig` 自動處理 source map upload。

### 2. CSP 實作位置：middleware
在 `middleware.ts` 使用 `NextResponse.headers.set()` 注入安全標頭。

**Rationale**: Next.js middleware 是唯一能在所有路由統一注入 response headers 的位置。`vercel.json` 的 headers 不支援動態 nonce 生成，但我們先用靜態 CSP 即可。

CSP 策略（寬鬆起步）：
- `default-src 'self'`
- `script-src 'self' 'unsafe-inline' 'unsafe-eval'` (Next.js 需要 inline script)
- `style-src 'self' 'unsafe-inline'` (Tailwind 需要)
- `img-src 'self' data: blob:`
- `connect-src 'self' *.sentry.io *.ingest.sentry.io` + API_URL + WS_URL
- `frame-ancestors 'none'`

### 3. vercel.json 最小化
僅設定必要項目：
- `headers`: 安全標頭 fallback（主要由 middleware 處理）
- 不設定 rewrites（API proxy 已用 Next.js route handler 實作）

### 4. DevTools 條件載入
使用 `next/dynamic` + `ssr: false` 條件載入 ReactQueryDevtools，僅在 development 環境渲染。

### 5. env 驗證 fail-fast
server 端 parse 加 `try/catch`，失敗時 `console.error` + `process.exit(1)`。確保缺少環境變數時 build/啟動直接失敗。

## Risks / Trade-offs

- **CSP `unsafe-inline`**: Next.js 目前需要 inline script，無法完全避免 `unsafe-inline`。未來可改用 nonce-based CSP。→ 先接受，比沒有 CSP 好。
- **Sentry DSN 外洩**: client-side DSN 是公開的（設計如此），Sentry 用 allowed domains 限制。→ 可接受的風險。
- **Sentry bundle size 增加**: ~30KB gzipped。→ 錯誤監控的價值遠大於體積成本。
