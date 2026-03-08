# frontend-types Specification

## Purpose
TBD - created by archiving change core-lib-types. Update Purpose after archive.
## Requirements
### Requirement: API response wrapper types
The types module SHALL define `ApiResponse<T>`, `ApiErrorResponse`, and `CursorPagination` interfaces matching the Go backend's unified response format.

#### Scenario: ApiResponse generic
- **WHEN** the backend returns `{ "data": T }`
- **THEN** `ApiResponse<T>` SHALL correctly type the wrapper with `data: T`

#### Scenario: CursorPagination
- **WHEN** a paginated endpoint returns pagination metadata
- **THEN** `CursorPagination` SHALL have `nextCursor?: string` and `hasMore: boolean`

### Requirement: Domain entity types
The types module SHALL define interfaces for all backend domain entities: `User`, `ApiKey`, `VerifyResult`, `UserConfig`, `StrategyConfig` (with nested `AmountConfig`, `RateConfig`, `PeriodConfig`), `DashboardSummary` (with nested `WalletSummary`, `OfferSummary`, `CreditSummary`, `MarketSummary`), `EarningsSummary`, `BillingRecord`, `PlanFeatures`, `ExecutionRecord`, `EngineStatus`.

#### Scenario: Type matches backend JSON
- **WHEN** Go backend serializes a domain struct to JSON (camelCase)
- **THEN** the corresponding TypeScript interface SHALL match field names and types exactly

### Requirement: List response types
The types module SHALL define composite list response types (`BillingListResponse`, `ExecutionListResponse`) that include both `data` array and `pagination` object.

#### Scenario: BillingListResponse structure
- **WHEN** the billing list endpoint returns `{ "data": BillingRecord[], "pagination": CursorPagination }`
- **THEN** `BillingListResponse` SHALL type this as `{ data: BillingRecord[]; pagination: CursorPagination }`

### Requirement: Environment variable validation
The `env.ts` module SHALL validate environment variables at startup using Zod schemas, with separate schemas for client (`NEXT_PUBLIC_*`) and server variables.

#### Scenario: Missing server env var
- **WHEN** `API_URL` is not set and code runs on the server
- **THEN** the Zod parse SHALL throw an error at startup (fail fast)

#### Scenario: Client-side validation
- **WHEN** code runs in the browser
- **THEN** only `NEXT_PUBLIC_*` variables SHALL be validated

### Requirement: Formatting utilities
The `format.ts` module SHALL provide formatting functions for the lending platform: `formatAPR(dailyRate)`, `formatDailyRate(rate)`, `formatUSD(amount)`, `formatPeriod(days)`.

#### Scenario: APR formatting
- **WHEN** calling `formatAPR(0.0001)` (daily rate)
- **THEN** the result SHALL be `"3.65%"` (dailyRate × 365 × 100, 2 decimal places)

#### Scenario: USD formatting
- **WHEN** calling `formatUSD(50000)`
- **THEN** the result SHALL use `Intl.NumberFormat` with `en-US` locale and `USD` currency

### Requirement: Query Key Factory
The `query-keys.ts` module SHALL export factory objects for each API resource (`apiKeyKeys`, `configKeys`, `billingKeys`, `executionKeys`, `dashboardKeys`, `earningsKeys`) returning `readonly` tuples for precise cache invalidation.

#### Scenario: Query key structure
- **WHEN** calling `billingKeys.list({ limit: 20 })`
- **THEN** the result SHALL be `["billing", "list", { limit: 20 }] as const`

