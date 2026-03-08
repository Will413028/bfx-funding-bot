## ADDED Requirements

### Requirement: BillingRecord domain type

The system SHALL define a `BillingRecord` struct in the `domain` package with fields: ID, UserID, PeriodStart, PeriodEnd, Amount, Currency, Status, PaidAt, CreatedAt.

#### Scenario: Valid status values

- **WHEN** creating a BillingRecord
- **THEN** Status SHALL be one of: `pending`, `paid`, `overdue`, `waived`

### Requirement: Billing records database table

The system SHALL have a `billing_records` table in PostgreSQL with appropriate columns, indexes, and foreign key to `users`.

#### Scenario: Index for per-user queries

- **GIVEN** the billing_records table
- **THEN** there SHALL be an index on `(user_id, period_start DESC)` for efficient per-user time-range queries

### Requirement: BillingRepository interface

The system SHALL define a `BillingRepository` interface in the `repository` package.

#### Scenario: Create record

- **WHEN** `Create(ctx, record)` is called with a valid BillingRecord
- **THEN** the record SHALL be persisted to the database

#### Scenario: List by user with time range

- **WHEN** `ListByUser(ctx, userID, since, limit)` is called
- **THEN** the system SHALL return billing records for that user since the given time, ordered by period_start DESC, limited to the specified count

### Requirement: Billing query API

The system SHALL expose `GET /api/v1/billing` (JWT protected) to query billing history with summary.

#### Scenario: Default query

- **WHEN** the endpoint is called without query params
- **THEN** the system SHALL return up to 12 records from the last 365 days, with a summary (total_paid, total_pending)

#### Scenario: Custom parameters

- **WHEN** the endpoint is called with `since` and `limit` query params
- **THEN** the system SHALL use those values (limit capped at 100)
