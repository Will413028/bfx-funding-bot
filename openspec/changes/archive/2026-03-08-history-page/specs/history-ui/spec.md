## ADDED Requirements

### Requirement: Executions infinite query hook
The `useExecutions` hook SHALL fetch execution records via `apiClient.getList<ExecutionListResponse>("/executions")` using TanStack Query `useInfiniteQuery` with `executionKeys.list()`. It SHALL pass `after` cursor param for pagination.

#### Scenario: First page fetch
- **WHEN** the hook is called without a cursor
- **THEN** it SHALL return the first page of execution records with pagination info

#### Scenario: Load more
- **WHEN** `fetchNextPage` is called and `hasMore` is true
- **THEN** it SHALL fetch the next page using `nextCursor` as the `after` param

### Requirement: Billing infinite query hook
The `useBilling` hook SHALL fetch billing records via `apiClient.getList<BillingListResponse>("/billing")` using TanStack Query `useInfiniteQuery` with `billingKeys.list()`. It SHALL pass `after` cursor param for pagination.

#### Scenario: First page fetch
- **WHEN** the hook is called without a cursor
- **THEN** it SHALL return the first page of billing records with pagination info

#### Scenario: Load more
- **WHEN** `fetchNextPage` is called and `hasMore` is true
- **THEN** it SHALL fetch the next page using `nextCursor` as the `after` param

### Requirement: Execution table display
The ExecutionTable component SHALL display execution records with columns: action (badge), currency, amount (formatUSD), rate (formatAPR), period (formatPeriod), status, and timestamp. Empty state SHALL show a muted message.

#### Scenario: Records displayed
- **WHEN** execution records exist
- **THEN** each row SHALL display action as a badge, amount formatted as USD, rate as APR, and period in days

#### Scenario: No records
- **WHEN** execution records are empty
- **THEN** the component SHALL display a "No execution records" empty state

### Requirement: Billing table display
The BillingTable component SHALL display billing records with columns: period range, plan (badge), amount (formatUSD), status (badge with color), and paid date. Empty state SHALL show a muted message.

#### Scenario: Records displayed
- **WHEN** billing records exist
- **THEN** each row SHALL display period range, plan badge, amount formatted as USD, and status with appropriate color

#### Scenario: Status colors
- **WHEN** displaying billing status
- **THEN** "paid" SHALL use `text-emerald-400`, "pending" SHALL use `text-amber-500`, "overdue" SHALL use `text-rose-500`, "waived" SHALL use `text-zinc-400`

### Requirement: Load More shared component
The LoadMoreButton component SHALL display a button to load additional records. It SHALL show a loading state when fetching and be hidden when no more records exist.

#### Scenario: More records available
- **WHEN** `hasMore` is true
- **THEN** the button SHALL be visible and clickable

#### Scenario: No more records
- **WHEN** `hasMore` is false
- **THEN** the button SHALL not be rendered

### Requirement: History page with Tab navigation
The History page SHALL display Executions and Billing tables in a Tab layout using shadcn/ui Tabs. Each tab SHALL independently manage its own pagination state.

#### Scenario: Default tab
- **WHEN** the page loads
- **THEN** the Executions tab SHALL be active by default

#### Scenario: Tab switching
- **WHEN** the user clicks the Billing tab
- **THEN** the Billing table SHALL be displayed without re-fetching Executions data
