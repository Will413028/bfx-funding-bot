## ADDED Requirements

### Requirement: ExecutionRecord domain type

The system SHALL define an `ExecutionRecord` struct in the `domain` package with fields: ID, UserID, Action, Currency, Amount, Rate, Period, OfferID, Status, ErrorMessage, CreatedAt.

#### Scenario: Valid action types

- **WHEN** creating an ExecutionRecord
- **THEN** Action SHALL be one of: `place`, `cancel`, `filled`, `renew`

### Requirement: Executions database table

The system SHALL have an `executions` table in PostgreSQL with appropriate columns, indexes, and foreign key to `users`.

#### Scenario: Index on user_id + created_at

- **GIVEN** the executions table
- **THEN** there SHALL be a composite index on `(user_id, created_at DESC)` for efficient per-user time-range queries

### Requirement: ExecutionRepository interface

The system SHALL define an `ExecutionRepository` interface in the `repository` package.

#### Scenario: Create record

- **WHEN** `Create(ctx, record)` is called with a valid ExecutionRecord
- **THEN** the record SHALL be persisted to the database

#### Scenario: List by user with time range

- **WHEN** `ListByUser(ctx, userID, since, limit)` is called
- **THEN** the system SHALL return execution records for that user since the given time, ordered by created_at DESC, limited to the specified count

### Requirement: Execution query API

The system SHALL expose `GET /api/v1/executions` (JWT protected) to query execution history.

#### Scenario: Query with default parameters

- **WHEN** the endpoint is called without query params
- **THEN** the system SHALL return up to 50 records from the last 7 days

#### Scenario: Query with custom parameters

- **WHEN** the endpoint is called with `since` and `limit` query params
- **THEN** the system SHALL use those values (limit capped at 200)

#### Scenario: Unauthorized access

- **WHEN** the endpoint is called without a valid JWT
- **THEN** the system SHALL return 401 Unauthorized
