## ADDED Requirements

### Requirement: Dashboard data hook
The `useDashboard` hook SHALL fetch DashboardSummary via `apiClient.get<DashboardSummary>("/dashboard")` using TanStack Query with `dashboardKeys.summary()`.

#### Scenario: Successful fetch
- **WHEN** the hook is called in a mounted component
- **THEN** it SHALL return `{ data: DashboardSummary, isLoading, isError }` from TanStack Query

### Requirement: Earnings data hook
The `useEarnings` hook SHALL fetch EarningsSummary via `apiClient.get<EarningsSummary>("/earnings")` using TanStack Query with `earningsKeys.summary()`.

#### Scenario: Successful fetch
- **WHEN** the hook is called in a mounted component
- **THEN** it SHALL return `{ data: EarningsSummary, isLoading, isError }` from TanStack Query

### Requirement: StatCard shared component
The StatCard component SHALL display an icon, label, formatted value, and optional subtitle. It SHALL use premium dark theme styling (`bg-white/[0.02] border border-white/5`) and financial typography (`tabular-nums tracking-tight`).

#### Scenario: USD value display
- **WHEN** displaying a USD amount like $50,000.00
- **THEN** the integer part SHALL use `text-foreground` and the decimal part SHALL use `text-zinc-500`

### Requirement: Stats grid with Bento Box layout
The stats grid SHALL display 4 stat cards (Total Lent, Available Balance, Est. Daily Earning, Worker Status) in a CSS Grid with responsive columns (1 col on mobile, 2 on sm, 4 on lg).

#### Scenario: Worker status display
- **WHEN** the engine is ready (engineReady: true)
- **THEN** the Worker Status card SHALL show "Running" with `text-emerald-400`

#### Scenario: Engine not ready
- **WHEN** engineReady is false
- **THEN** the Worker Status card SHALL show "Stopped" with `text-rose-500`

### Requirement: Offers list panel
The OffersList component SHALL display active funding offers with amount, rate (APY as primary, daily rate as secondary), and period badge. Empty state SHALL show a muted message.

#### Scenario: Offers displayed
- **WHEN** the dashboard has active offers
- **THEN** each offer SHALL show amount (formatUSD), rate (formatAPR as primary), and period (badge with formatPeriod)

#### Scenario: No offers
- **WHEN** offers array is empty
- **THEN** the component SHALL display a "No active offers" empty state

### Requirement: Market snapshot panel
The MarketPanel component SHALL display FRR (as APR), market regime, MDC score, and flash freeze status with appropriate status colors.

#### Scenario: Normal market
- **WHEN** flash freeze is false and regime is "bull"
- **THEN** FRR SHALL display in `text-emerald-400`, regime SHALL show as a badge

#### Scenario: Flash freeze active
- **WHEN** flash freeze is true
- **THEN** the flash freeze indicator SHALL display in `text-rose-500`

### Requirement: Chart placeholder
The chart placeholder SHALL occupy a full-width grid area with a dashed border and centered text indicating that charts will be available in a future phase.

#### Scenario: Placeholder display
- **WHEN** the overview page renders
- **THEN** the chart area SHALL show a dashed border container with placeholder text
