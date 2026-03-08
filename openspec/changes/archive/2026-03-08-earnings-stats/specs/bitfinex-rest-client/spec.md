## ADDED Requirements

### Requirement: Funding earnings ledger query
The system SHALL provide a `GetFundingEarnings` method on the Bitfinex client that retrieves funding interest payment history from the Ledger API.

#### Scenario: Query ledger with time range
- **WHEN** `GetFundingEarnings` is called with a currency, start timestamp, and end timestamp
- **THEN** the system SHALL call `POST /v2/auth/r/ledgers/{currency}/hist` with `category: 28`, `start`, `end`, and `limit: 2500`, and return parsed `FundingEarning` entries

#### Scenario: Empty ledger history
- **WHEN** the ledger API returns an empty array
- **THEN** the system SHALL return an empty slice of `FundingEarning`

#### Scenario: Parse ledger response
- **WHEN** the ledger API returns entries
- **THEN** the system SHALL parse each entry as `[ID, CURRENCY, null, MTS, null, AMOUNT, BALANCE, null, DESCRIPTION]` and map to `FundingEarning` domain model
