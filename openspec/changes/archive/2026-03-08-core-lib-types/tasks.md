## 1. Types

- [x] 1.1 Create `src/types/index.ts` — API response wrappers (`ApiResponse<T>`, `ApiErrorResponse`, `CursorPagination`), all domain entity interfaces (`User`, `ApiKey`, `VerifyResult`, `UserConfig`, `StrategyConfig`, `DashboardSummary`, `EarningsSummary`, `BillingRecord`, `PlanFeatures`, `ExecutionRecord`, `EngineStatus`), list response types (`BillingListResponse`, `ExecutionListResponse`)

## 2. API Client

- [x] 2.1 Create `src/lib/api-client.ts` — `ApiError` class (extends Error, with status/code/message), `getBaseUrl()` dual-environment resolver, internal `request()` helper with JSON parsing + error handling
- [x] 2.2 Add `get<T>()`, `getList<T>()`, `post<T>()`, `put<T>()`, `del<T>()` methods with query string support and auth header forwarding

## 3. Utilities

- [x] 3.1 Create `src/lib/format.ts` — `formatAPR()`, `formatDailyRate()`, `formatUSD()`, `formatPeriod()`
- [x] 3.2 Create `src/lib/env.ts` — `clientEnvSchema` + `serverEnvSchema` (Zod), runtime-aware export
- [x] 3.3 Create `src/lib/query-keys.ts` — Query Key Factory for all resources (`apiKeyKeys`, `configKeys`, `billingKeys`, `executionKeys`, `dashboardKeys`, `earningsKeys`)

## 4. Verification

- [x] 4.1 Run `pnpm build` — verify production build succeeds
- [x] 4.2 Run `pnpm lint` — verify tsc + Biome pass
