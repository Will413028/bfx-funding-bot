## MODIFIED Requirements

### Requirement: Periodic tick loop

The system SHALL execute a lending cycle every 3 minutes using a `time.Ticker`.

#### Scenario: Tick interval

- **WHEN** the engine is running
- **THEN** it SHALL execute a tick every 3 minutes

#### Scenario: Tick processes all active users

- **WHEN** a tick executes
- **THEN** the engine SHALL query all users with verified API keys and strategy configs, and process each user serially

#### Scenario: Single user failure does not affect others

- **WHEN** processing a user fails (API error, invalid key, etc.)
- **THEN** the engine SHALL log the error and continue processing the next user

#### Scenario: Tick acquires quota before fetching

- **WHEN** a worker tick begins for a user
- **THEN** the worker SHALL call `quota.Acquire(userID, 3)` before making API calls. If acquire returns false, the tick SHALL be skipped with a log warning.

## ADDED Requirements

### Requirement: Worker deps include per-user rate limiter

The `DepsFactory.BuildWorkerDeps()` SHALL obtain a per-user `rate.Limiter` from `RateLimiterPool` and inject it into the `DataFetcher` and `OfferExecutor`.

#### Scenario: Fetcher receives limiter

- **WHEN** `BuildWorkerDeps(userID, currency, snapshotCh)` is called
- **THEN** the returned `Deps.Fetcher` SHALL use the user's rate limiter for all API calls

#### Scenario: Executor receives limiter

- **WHEN** `BuildWorkerDeps(userID, currency, snapshotCh)` is called
- **THEN** the returned `Deps.Executor` SHALL use the user's rate limiter for all API calls

### Requirement: Dashboard and Earnings services use per-user rate limiting

`DashboardService` and `EarningsService` SHALL hold a reference to `RateLimiterPool` and obtain the user's rate limiter before making Bitfinex API calls.

#### Scenario: Dashboard uses per-user limiter

- **WHEN** `DashboardService.GetSummary(ctx, userID)` is called
- **THEN** all Bitfinex API calls SHALL wait on the user's rate limiter

#### Scenario: Earnings uses per-user limiter

- **WHEN** `EarningsService.GetEarnings(ctx, userID)` is called
- **THEN** all Bitfinex API calls SHALL wait on the user's rate limiter

#### Scenario: No limiter in pool for dashboard user

- **WHEN** `DashboardService.GetSummary()` is called for a user without an active worker (no limiter in pool)
- **THEN** the service SHALL create a temporary limiter via `pool.Get(userID)` and use it
