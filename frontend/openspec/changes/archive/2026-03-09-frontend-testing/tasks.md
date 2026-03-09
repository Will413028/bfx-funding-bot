## 1. Vitest 設定

- [x] 1.1 安裝 vitest，建立 vitest.config.ts，package.json 加入 `test` script
- [x] 1.2 建立 `src/lib/__tests__/format.test.ts`（formatAPR, formatDailyRate, formatUSD, formatPeriod）
- [x] 1.3 建立 `src/lib/__tests__/query-keys.test.ts`（所有 key factory 函式）
- [x] 1.4 建立 `src/lib/__tests__/utils.test.ts`（cn 合併 + falsy 處理）
- [x] 1.5 建立 `src/lib/__tests__/validations.test.ts`（loginSchema, registerSchema, strategyConfigSchema 通過/失敗案例）
- [x] 1.6 建立 `src/lib/__tests__/api-client.test.ts`（mock fetch: get/post 成功 + ApiError 分支）

## 2. Playwright 設定

- [x] 2.1 安裝 @playwright/test，建立 playwright.config.ts，package.json 加入 `test:e2e` script
- [x] 2.2 建立 `e2e/smoke.spec.ts`（landing page + login page 載入 + i18n 切換）

## 3. 驗證

- [x] 3.1 `pnpm test` 全部通過 + `tsc --noEmit` + Biome check
