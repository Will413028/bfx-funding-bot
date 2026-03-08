## 1. Dependencies

- [x] 1.1 Install shadcn/ui tabs: `npx shadcn@latest add tabs` + auto-fix Biome

## 2. Shared Components

- [x] 2.1 Create `src/components/shared/load-more-button.tsx` — LoadMoreButton (hasMore, isFetchingNextPage, onClick props)

## 3. Data Hooks

- [x] 3.1 Create `src/features/history/hooks/use-executions.ts` — useInfiniteQuery + apiClient.getList + executionKeys.list + cursor pagination
- [x] 3.2 Create `src/features/history/hooks/use-billing.ts` — useInfiniteQuery + apiClient.getList + billingKeys.list + cursor pagination

## 4. Components

- [x] 4.1 Create `src/features/history/components/execution-table.tsx` — div grid table, action badge, amount USD, rate APR, period, status, timestamp, empty state
- [x] 4.2 Create `src/features/history/components/billing-table.tsx` — div grid table, period range, plan badge, amount USD, status badge (paid/pending/overdue/waived colors), paidAt, empty state

## 5. Page

- [x] 5.1 Create `src/app/[locale]/(dashboard)/history/page.tsx` — "use client", Tabs (Executions default / Billing), LoadMoreButton per tab, loading state

## 6. Verification

- [x] 6.1 Run `pnpm build` — verify production build succeeds
- [x] 6.2 Run `pnpm lint` — verify tsc + Biome pass
