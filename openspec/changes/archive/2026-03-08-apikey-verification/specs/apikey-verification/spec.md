## ADDED Requirements

### Requirement: Verify stored API Key
The system SHALL provide an endpoint to verify a stored Bitfinex API Key by calling the Bitfinex API. Upon successful verification, the system SHALL update the key's `exchange_status` to `verified` and return the funding wallet balance.

#### Scenario: Successful verification
- **WHEN** an authenticated user sends POST `/api/v1/apikeys/:id/verify` for their own key
- **THEN** the system SHALL call `bitfinex.Client.VerifyCredentials`, update `exchange_status` to `verified`, and return HTTP 200 with `{ "status": "verified", "funding_balance": { "currency": "USD", "balance": 1000, "available": 800 } }`

#### Scenario: Invalid API Key
- **WHEN** the Bitfinex API returns an authentication error
- **THEN** the system SHALL update `exchange_status` to `unverified` and return HTTP 200 with `{ "status": "unverified", "error": "Invalid or expired Bitfinex API key" }`

#### Scenario: Key not found
- **WHEN** the key ID does not exist or belongs to another user
- **THEN** the system SHALL return HTTP 404 with error code `NOT_FOUND`

### Requirement: Auto-verify on creation
The system SHALL automatically verify the API Key when it is created. The verification result SHALL be stored as `exchange_status` in the database.

#### Scenario: Create with valid key
- **WHEN** an authenticated user creates an API Key with valid Bitfinex credentials
- **THEN** the system SHALL store the key with `exchange_status = "verified"` and include verification result in the response

#### Scenario: Create with invalid key
- **WHEN** an authenticated user creates an API Key with invalid Bitfinex credentials
- **THEN** the system SHALL still store the key but with `exchange_status = "unverified"`

#### Scenario: Bitfinex API unavailable
- **WHEN** the Bitfinex API is unreachable during creation
- **THEN** the system SHALL still store the key with `exchange_status = "unverified"`

### Requirement: Exchange status tracking
The system SHALL track the verification status of each API Key via an `exchange_status` column in the `api_keys` table.

#### Scenario: Status values
- **WHEN** an API Key is stored
- **THEN** the `exchange_status` SHALL be one of: `verified`, `unverified`

#### Scenario: Status visible in responses
- **WHEN** an API Key is returned via any endpoint (create, get, list)
- **THEN** the response SHALL include the `exchange_status` field
