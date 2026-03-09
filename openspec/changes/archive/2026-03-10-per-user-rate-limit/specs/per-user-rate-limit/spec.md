## ADDED Requirements

### Requirement: Per-user rate limiter pool

The system SHALL provide a `RateLimiterPool` that manages per-user `rate.Limiter` instances. Each user SHALL have an independent rate limiter with configurable rate and burst parameters.

#### Scenario: Get limiter for existing user

- **WHEN** `Get(userID)` is called for a user that already has a limiter
- **THEN** the pool SHALL return the same `*rate.Limiter` instance

#### Scenario: Get limiter for new user

- **WHEN** `Get(userID)` is called for a user that does not yet have a limiter
- **THEN** the pool SHALL create a new `rate.Limiter` with the pool's default rate and burst, store it, and return it

#### Scenario: Remove user limiter

- **WHEN** `Remove(userID)` is called
- **THEN** the pool SHALL delete the user's limiter from the pool

#### Scenario: Concurrent access

- **WHEN** multiple goroutines call `Get()` for different users concurrently
- **THEN** the pool SHALL be safe for concurrent use without data races

### Requirement: Per-user rate limiting on Bitfinex API calls

All Bitfinex REST API calls made on behalf of a user SHALL be rate-limited by the user's per-user `rate.Limiter`. The caller MUST call `limiter.Wait(ctx)` before each API request.

#### Scenario: Fetcher respects per-user limiter

- **WHEN** `userDataFetcher.FetchUserData()` makes 3 parallel API calls for a user
- **THEN** each call SHALL wait on the user's rate limiter before proceeding

#### Scenario: Executor respects per-user limiter

- **WHEN** `OfferExecutor.Execute()` submits a funding offer for a user
- **THEN** it SHALL wait on the user's rate limiter before the API call

#### Scenario: Dashboard respects per-user limiter

- **WHEN** `DashboardService.GetSummary()` fetches wallet/offers/credits for a user
- **THEN** each API call SHALL wait on the user's rate limiter

#### Scenario: Earnings respects per-user limiter

- **WHEN** `EarningsService.GetEarnings()` fetches credits and earnings for a user
- **THEN** each API call SHALL wait on the user's rate limiter

### Requirement: Pool lifecycle tied to worker lifecycle

The `RateLimiterPool` SHALL create a limiter when a worker starts for a user and remove it when the worker stops.

#### Scenario: Worker start creates limiter

- **WHEN** `lending.Service.StartWorker(userID)` is called
- **THEN** the pool SHALL have a limiter entry for that userID

#### Scenario: Worker stop removes limiter

- **WHEN** `lending.Service.StopWorker(userID)` is called
- **THEN** the pool SHALL remove the limiter entry for that userID

### Requirement: Configurable per-user rate and burst

The `RateLimiterPool` SHALL accept default rate (requests per second) and burst size as constructor parameters.

#### Scenario: Custom rate configuration

- **WHEN** `NewRateLimiterPool(rate=2.0, burst=6)` is called
- **THEN** all new user limiters created via `Get()` SHALL use rate=2.0 and burst=6
