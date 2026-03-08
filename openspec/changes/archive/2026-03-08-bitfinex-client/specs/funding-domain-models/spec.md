## ADDED Requirements

### Requirement: FundingOffer domain model
The system SHALL define a `domain.FundingOffer` struct with:
- `ID` (int64) — Bitfinex offer ID
- `Currency` (string) — e.g., "USD", "UST"
- `Amount` (float64) — offer amount
- `Rate` (float64) — daily rate as decimal fraction
- `Period` (int) — lending period in days
- `Status` (string) — offer status ("ACTIVE", "EXECUTED", "PARTIALLY FILLED", "CANCELED")
- `CreatedAt` (time.Time)
- `UpdatedAt` (time.Time)

#### Scenario: FundingOffer used across layers
- **WHEN** a funding offer is returned from bitfinex client
- **THEN** it SHALL be represented as `domain.FundingOffer` in all upper layers

### Requirement: OfferParams domain model
The system SHALL define a `domain.OfferParams` struct for submitting new offers:
- `Currency` (string, required)
- `Amount` (float64, required) — positive value
- `Rate` (float64, required) — daily rate
- `Period` (int, required) — 2-120 days
- `AutoRenew` (bool)

#### Scenario: OfferParams used for submission
- **WHEN** the lending engine or service wants to submit a funding offer
- **THEN** it SHALL construct `domain.OfferParams` and pass to `bitfinex.Client.SubmitFundingOffer()`

### Requirement: FundingCredit domain model
The system SHALL define a `domain.FundingCredit` struct with:
- `ID` (int64) — Bitfinex credit ID
- `Currency` (string)
- `Amount` (float64)
- `Rate` (float64) — daily rate
- `Period` (int)
- `Status` (string) — "ACTIVE", "CLOSED"
- `AutoRenew` (bool)
- `OpenedAt` (time.Time)
- `ExpiresAt` (time.Time)

#### Scenario: FundingCredit represents active loan
- **WHEN** a user's funding has been borrowed by someone
- **THEN** it SHALL be represented as `domain.FundingCredit`

### Requirement: Wallet domain model
The system SHALL define a `domain.Wallet` struct with:
- `Currency` (string)
- `Balance` (float64) — total balance
- `BalanceAvailable` (float64) — available for new offers

#### Scenario: Wallet represents funding wallet
- **WHEN** querying a user's funding wallet
- **THEN** the `domain.Wallet` SHALL distinguish between total balance and available balance (excluding locked amounts in active offers/credits)
