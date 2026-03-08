## ADDED Requirements

### Requirement: Earnings summary endpoint
The system SHALL provide a `GET /api/v1/earnings` endpoint that returns the user's funding earnings statistics. The endpoint SHALL require JWT authentication.

#### Scenario: User with active credits and history
- **WHEN** the authenticated user has a verified API key with active credits and historical earnings
- **THEN** the system SHALL return `estimated_daily_earning`, `weighted_apy`, `earnings_7d`, `earnings_30d`, and `total_earned_amount` from active credits

#### Scenario: User without API key
- **WHEN** the authenticated user has no API key stored
- **THEN** the system SHALL return a response with all values set to zero

#### Scenario: User with no active credits
- **WHEN** the authenticated user has a verified API key but no active credits
- **THEN** the system SHALL return `estimated_daily_earning: 0`, `weighted_apy: 0`, and historical earnings from ledger

### Requirement: Estimated daily earnings calculation
The system SHALL calculate estimated daily earnings from active funding credits.

#### Scenario: Calculate from active credits
- **WHEN** the user has active credits with amounts and rates
- **THEN** the system SHALL compute `estimated_daily_earning = sum(credit.Amount * credit.Rate)` for all active credits

### Requirement: Weighted APY calculation
The system SHALL calculate the weighted average annual percentage yield from active credits.

#### Scenario: Calculate weighted APY
- **WHEN** the user has active credits
- **THEN** the system SHALL compute `weighted_apy = (sum(credit.Amount * credit.Rate) / sum(credit.Amount)) * 365`

#### Scenario: No active credits
- **WHEN** the user has no active credits
- **THEN** the system SHALL return `weighted_apy: 0`

### Requirement: Historical earnings from Ledger API
The system SHALL retrieve historical funding earnings from the Bitfinex Ledger API.

#### Scenario: Retrieve 7-day earnings
- **WHEN** the earnings endpoint is called
- **THEN** the system SHALL sum all "Margin Funding Payment" ledger entries from the past 7 days

#### Scenario: Retrieve 30-day earnings
- **WHEN** the earnings endpoint is called
- **THEN** the system SHALL sum all "Margin Funding Payment" ledger entries from the past 30 days

### Requirement: Parallel API aggregation
The system SHALL call Bitfinex credits and ledger endpoints in parallel to minimize latency.

#### Scenario: Both API calls succeed
- **WHEN** both credits and ledger API calls succeed
- **THEN** the system SHALL return complete earnings data

#### Scenario: Partial failure
- **WHEN** one API call fails
- **THEN** the system SHALL return the successfully retrieved data and set failed sections to zero
