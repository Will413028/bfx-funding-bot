## ADDED Requirements

### Requirement: Dashboard summary endpoint
The system SHALL provide a `GET /api/v1/dashboard` endpoint that returns the user's funding status summary. The endpoint SHALL require JWT authentication.

#### Scenario: User with verified key and config
- **WHEN** the authenticated user has a verified API key and a strategy config
- **THEN** the system SHALL return a JSON response with `wallet` (balance, available), `offers` (active funding offers array), `credits` (active funding credits array), and `engine_ready: true`

#### Scenario: User without API key
- **WHEN** the authenticated user has no API key stored
- **THEN** the system SHALL return a response with `wallet: null`, `offers: []`, `credits: []`, and `engine_ready: false`

#### Scenario: User with unverified API key
- **WHEN** the authenticated user has an API key with `exchange_status != "verified"`
- **THEN** the system SHALL return a response with `wallet: null`, `offers: []`, `credits: []`, and `engine_ready: false`

#### Scenario: User without strategy config
- **WHEN** the authenticated user has a verified API key but no strategy config
- **THEN** the system SHALL return wallet and funding data from Bitfinex, and `engine_ready: false`

### Requirement: Parallel Bitfinex API aggregation
The system SHALL call Bitfinex API endpoints (`wallets`, `offers/fCURRENCY`, `credits/fCURRENCY`) in parallel using errgroup to minimize latency.

#### Scenario: All API calls succeed
- **WHEN** all three Bitfinex API calls succeed
- **THEN** the system SHALL return the complete dashboard data in a single response

#### Scenario: Partial API failure
- **WHEN** one or more Bitfinex API calls fail
- **THEN** the system SHALL return the successfully retrieved data and set failed sections to null/empty, without returning an HTTP error

### Requirement: Market snapshot from Redis cache
The system SHALL read the latest MarketSnapshot from Redis cache (via SnapshotCache) and include it in the dashboard response. The cache key is derived from the user's configured currency (e.g., `fUSD`).

#### Scenario: Snapshot available
- **WHEN** a MarketSnapshot exists in Redis for the user's currency
- **THEN** the response SHALL include `market` with `frr`, `regime`, `mdc_score`, `flash_freeze`, and `timestamp`

#### Scenario: Snapshot unavailable
- **WHEN** no MarketSnapshot exists in Redis (cache miss or engine not running)
- **THEN** the response SHALL set `market: null` without returning an HTTP error

### Requirement: Dashboard response format
The system SHALL return the dashboard data in a structured JSON format.

#### Scenario: Complete response structure
- **WHEN** the dashboard endpoint is called successfully
- **THEN** the response SHALL contain:
  - `wallet`: `{ currency, balance, balance_available }` or null
  - `offers`: array of `{ id, currency, amount, rate, period, status, created_at }`
  - `credits`: array of `{ id, currency, amount, rate, period, status, auto_renew, opened_at }`
  - `market`: `{ frr, regime, mdc_score, flash_freeze, timestamp }` or null
  - `engine_ready`: boolean indicating if the lending engine is active for this user
