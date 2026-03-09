## ADDED Requirements

### Requirement: Sentry SDK initialization
系統 SHALL 在 client、server、edge 三層分別初始化 Sentry SDK，使用 `NEXT_PUBLIC_SENTRY_DSN` 環境變數。

#### Scenario: Client-side error capture
- **WHEN** client-side JavaScript 拋出未捕捉的錯誤
- **THEN** Sentry SDK 自動捕捉並回報至 Sentry 專案

#### Scenario: Server-side error capture
- **WHEN** API route handler 或 Server Component 拋出錯誤
- **THEN** Sentry SDK 在 server 層捕捉並回報

#### Scenario: DSN 未設定
- **WHEN** `NEXT_PUBLIC_SENTRY_DSN` 環境變數為空或未設定
- **THEN** Sentry SDK 不初始化，應用程式正常運作不受影響

### Requirement: Source map upload
`next.config.ts` SHALL 使用 `withSentryConfig` 包裝，在 CI build 時自動上傳 source map 至 Sentry。

#### Scenario: Production build with Sentry
- **WHEN** 執行 `next build` 且 `SENTRY_AUTH_TOKEN` 已設定
- **THEN** source map 自動上傳至 Sentry，client bundle 不含 source map

#### Scenario: Local build without Sentry token
- **WHEN** 執行 `next build` 且 `SENTRY_AUTH_TOKEN` 未設定
- **THEN** build 正常完成，跳過 source map upload（silent mode）

### Requirement: DevTools production 隔離
ReactQueryDevtools SHALL 僅在 `NODE_ENV === "development"` 時載入。

#### Scenario: Development 環境
- **WHEN** `NODE_ENV` 為 `development`
- **THEN** ReactQueryDevtools 面板可見

#### Scenario: Production 環境
- **WHEN** `NODE_ENV` 為 `production`
- **THEN** ReactQueryDevtools 不載入、不渲染、不佔 bundle size
