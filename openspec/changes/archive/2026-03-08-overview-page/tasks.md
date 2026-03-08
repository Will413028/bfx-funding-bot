## 1. Dependencies

- [x] 1.1 Install shadcn/ui badge: `npx shadcn@latest add badge` + auto-fix Biome

## 2. Data Hooks

- [x] 2.1 Create `src/features/dashboard/hooks/use-dashboard.ts` — useDashboard hook (TanStack Query + apiClient.get + dashboardKeys.summary)
- [x] 2.2 Create `src/features/dashboard/hooks/use-earnings.ts` — useEarnings hook (TanStack Query + apiClient.get + earningsKeys.summary)

## 3. Shared Components

- [x] 3.1 Create `src/components/shared/stat-card.tsx` — StatCard (icon, label, value, subtitle, premium dark card styling, tabular-nums)

## 4. Dashboard Feature Components

- [x] 4.1 Create `src/features/dashboard/components/stats-grid.tsx` — 4 stat cards in responsive CSS Grid (Total Lent, Available Balance, Est. Daily Earning, Worker Status), USD decimal dimming, status colors
- [x] 4.2 Create `src/features/dashboard/components/offers-list.tsx` — active offers panel (amount, APY primary + daily rate secondary, period badge), empty state
- [x] 4.3 Create `src/features/dashboard/components/market-panel.tsx` — market snapshot (FRR as APR, regime badge, MDC score, flash freeze status)
- [x] 4.4 Create `src/features/dashboard/components/chart-placeholder.tsx` — dashed border placeholder for future Recharts

## 5. Overview Page

- [x] 5.1 Update `src/app/[locale]/(dashboard)/overview/page.tsx` — "use client", useDashboard + useEarnings, Bento Box grid layout combining StatsGrid + ChartPlaceholder + OffersList + MarketPanel, loading/error states

## 6. Verification

- [x] 6.1 Run `pnpm build` — verify production build succeeds
- [x] 6.2 Run `pnpm lint` — verify tsc + Biome pass
