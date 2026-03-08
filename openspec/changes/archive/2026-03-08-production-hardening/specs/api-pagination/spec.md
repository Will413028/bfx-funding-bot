## ADDED Requirements

### Requirement: Cursor-based pagination for executions
The system SHALL support cursor-based pagination on GET /executions using `created_at` + `id` as the cursor key, with configurable limit (default 20, max 100).

#### Scenario: First page without cursor
- **WHEN** GET /executions is called without `after` parameter
- **THEN** the response returns the most recent records (up to `limit`), ordered by `created_at DESC`, with a `pagination` object containing `next_cursor` and `has_more`

#### Scenario: Subsequent page with cursor
- **WHEN** GET /executions is called with `after=<cursor>` parameter
- **THEN** the response returns records older than the cursor position, maintaining consistent ordering

#### Scenario: Last page
- **WHEN** GET /executions returns fewer records than `limit`
- **THEN** `pagination.has_more` is `false` and `pagination.next_cursor` is empty

#### Scenario: Limit validation
- **WHEN** GET /executions is called with `limit` > 100 or `limit` < 1
- **THEN** the system clamps the value to the valid range (1-100)

### Requirement: Cursor-based pagination for billing records
The system SHALL support cursor-based pagination on GET /billing using the same cursor mechanism as executions.

#### Scenario: First page without cursor
- **WHEN** GET /billing is called without `after` parameter
- **THEN** the response returns the most recent billing records (up to `limit`) with pagination metadata

#### Scenario: Subsequent page with cursor
- **WHEN** GET /billing is called with `after=<cursor>` parameter
- **THEN** the response returns billing records older than the cursor position
