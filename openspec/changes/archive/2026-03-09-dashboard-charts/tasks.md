## 1. Backend — Earnings History API

- [x] 1.1 Add `DailyEarning` type to `service/earnings.go` and `GetEarningsHistory(ctx, userID, days)` method — fetch from Bitfinex, bucket by UTC date, fill gaps with 0, return ascending
- [x] 1.2 Add `History` handler to `handler/earnings.go` — parse `days` query param (default 30, max 90), call service, return `{"data":[...]}`
- [x] 1.3 Update `handler/router.go` — register `GET /earnings/history` in protected group
- [x] 1.4 Run `go build ./...` and `go test ./...` — verify backend compiles and tests pass

## 2. Frontend — Setup

- [x] 2.1 Install `recharts` package
- [x] 2.2 Add `DailyEarning` type to `types/index.ts` and `useEarningsHistory` hook with TanStack Query (staleTime 5min)

## 3. Frontend — Chart Components

- [x] 3.1 Create `src/features/dashboard/components/earnings-chart.tsx` — AreaChart from earnings history API, premium dark theme, responsive
- [x] 3.2 Create `src/features/dashboard/components/rate-chart.tsx` — AreaChart from WS store FRR ring buffer (max 60 points), premium dark theme, responsive

## 4. Frontend — Integration

- [x] 4.1 Update `src/app/[locale]/(dashboard)/overview/page.tsx` — replace ChartPlaceholder import with RateChart + EarningsChart in 2-column grid
- [x] 4.2 Delete `src/features/dashboard/components/chart-placeholder.tsx`

## 5. Verification

- [x] 5.1 Run `pnpm build` — verify production build succeeds
- [x] 5.2 Run `pnpm lint` — verify tsc + Biome pass
