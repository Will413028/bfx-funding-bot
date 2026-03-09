## 1. Sentry 整合

- [x] 1.1 安裝 `@sentry/nextjs` 並執行初始化（sentry.client.config.ts, sentry.server.config.ts, sentry.edge.config.ts）
- [x] 1.2 啟用 next.config.ts 的 `withSentryConfig` 包裝（取消註解 + 更新設定）
- [x] 1.3 env.ts 加入 `NEXT_PUBLIC_SENTRY_DSN`（optional，空值時不初始化）

## 2. 安全標頭

- [x] 2.1 middleware.ts 加入 CSP + 安全標頭注入邏輯（排除 /api/ 路徑）
- [x] 2.2 加入 HSTS header

## 3. 部署設定

- [x] 3.1 建立 `vercel.json`（最小設定）
- [x] 3.2 env.ts 改為 fail-fast 模式（server 端 parse 失敗 → process.exit(1)）

## 4. DevTools 隔離

- [x] 4.1 query-provider.tsx 條件載入 ReactQueryDevtools（僅 development）

## 5. 驗證

- [x] 5.1 `next build` 通過 + `tsc --noEmit` + Biome check
