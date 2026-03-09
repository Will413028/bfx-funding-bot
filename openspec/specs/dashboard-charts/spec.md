## ADDED Requirements

### Requirement: Earnings history API endpoint
The system SHALL provide a `GET /api/v1/earnings/history` endpoint that returns daily earnings as a time-series array.

#### Scenario: Default 30-day history
- **WHEN** an authenticated user calls `GET /api/v1/earnings/history`
- **THEN** the server SHALL return the last 30 days of daily earnings as `{"data":[{"date":"YYYY-MM-DD","amount":N},...]}`

#### Scenario: Custom days parameter
- **WHEN** a user calls `GET /api/v1/earnings/history?days=7`
- **THEN** the server SHALL return the last 7 days of daily earnings

#### Scenario: Days parameter exceeds maximum
- **WHEN** a user calls with `days` greater than 90
- **THEN** the server SHALL cap the value at 90

#### Scenario: No API key configured
- **WHEN** a user has no verified API key
- **THEN** the server SHALL return an empty data array

### Requirement: Earnings trend chart
The Overview page SHALL display an area chart showing daily earnings over the last 30 days, with data from the earnings history endpoint.

#### Scenario: Data loaded
- **WHEN** earnings history data is available
- **THEN** the chart SHALL display an area chart with date on X-axis and USD amount on Y-axis

#### Scenario: Loading state
- **WHEN** data is still loading
- **THEN** the chart SHALL show a loading indicator

### Requirement: Rate trend chart
The Overview page SHALL display a real-time FRR trend chart using data accumulated from WebSocket snapshots.

#### Scenario: WS connected with data
- **WHEN** WebSocket snapshots are received
- **THEN** the chart SHALL display an area chart of FRR (APR) values

#### Scenario: No data available
- **WHEN** no snapshots have been received
- **THEN** the chart SHALL display a placeholder message

### Requirement: Chart placeholder removal
The ChartPlaceholder component SHALL be replaced by the actual chart components in a 2-column grid layout.

#### Scenario: Overview page renders
- **WHEN** the Overview page loads
- **THEN** rate chart and earnings chart SHALL display in a 2-column grid
