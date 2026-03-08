## ADDED Requirements

### Requirement: Bitfinex client struct
The system SHALL define a `bitfinex.Client` struct in `internal/bitfinex/client.go` that wraps Bitfinex REST API v2 using Go stdlib `net/http`.

#### Scenario: Client construction
- **WHEN** `bitfinex.NewClient(httpClient)` is called with an `*http.Client`
- **THEN** it SHALL return a `*Client` struct ready to make API calls

### Requirement: HMAC-SHA384 authentication
The system SHALL implement Bitfinex API v2 authentication in `internal/bitfinex/auth.go`:
- Compute `HMAC-SHA384("/api/v2/<path>" + nonce + body, apiSecret)`
- Set headers: `bfx-apikey`, `bfx-nonce`, `bfx-signature`
- Nonce SHALL be monotonically increasing (millisecond Unix timestamp + atomic counter)

#### Scenario: Authenticated request
- **WHEN** an authenticated API call is made
- **THEN** the system SHALL compute the HMAC signature and set all required headers

### Requirement: Verify API key permissions
The system SHALL provide a method to verify that a Bitfinex API key is valid by calling an authenticated endpoint.

#### Scenario: Valid key
- **WHEN** `client.VerifyCredentials(ctx, apiKey, apiSecret)` is called with valid credentials
- **THEN** it SHALL return nil error

#### Scenario: Invalid key
- **WHEN** `client.VerifyCredentials(ctx, apiKey, apiSecret)` is called with invalid credentials
- **THEN** it SHALL return a descriptive error

### Requirement: Get funding wallet balance
The system SHALL provide a method to query the user's funding wallet balance for a specific currency.

#### Scenario: Wallet has balance
- **WHEN** `client.GetFundingBalance(ctx, apiKey, apiSecret, currency)` is called
- **THEN** it SHALL return a `domain.Wallet` with available and total balance

#### Scenario: API error
- **WHEN** the Bitfinex API returns an error
- **THEN** the method SHALL return a wrapped error with context

### Requirement: Submit funding offer
The system SHALL provide a method to submit a new funding offer via `POST /v2/auth/w/funding/offer/submit`.

#### Scenario: Successful submission
- **WHEN** `client.SubmitFundingOffer(ctx, apiKey, apiSecret, params)` is called with valid `domain.OfferParams`
- **THEN** it SHALL submit the offer and return the created `domain.FundingOffer`

#### Scenario: Insufficient balance
- **WHEN** the user does not have enough balance
- **THEN** the method SHALL return an error indicating insufficient funds

### Requirement: Cancel funding offer
The system SHALL provide a method to cancel an active funding offer via `POST /v2/auth/w/funding/offer/cancel`.

#### Scenario: Successful cancellation
- **WHEN** `client.CancelFundingOffer(ctx, apiKey, apiSecret, offerID)` is called with a valid offer ID
- **THEN** it SHALL cancel the offer and return nil error

#### Scenario: Offer not found
- **WHEN** the offer ID does not exist or is already filled/cancelled
- **THEN** the method SHALL return an error

### Requirement: List active funding offers
The system SHALL provide a method to list active funding offers via `POST /v2/auth/r/funding/offers/<currency>`.

#### Scenario: Has active offers
- **WHEN** `client.GetActiveFundingOffers(ctx, apiKey, apiSecret, currency)` is called and there are active offers
- **THEN** it SHALL return a slice of `domain.FundingOffer`

#### Scenario: No active offers
- **WHEN** there are no active offers
- **THEN** it SHALL return an empty slice (not nil)

### Requirement: List active funding credits
The system SHALL provide a method to list active funding credits via `POST /v2/auth/r/funding/credits/<currency>`.

#### Scenario: Has active credits
- **WHEN** `client.GetActiveFundingCredits(ctx, apiKey, apiSecret, currency)` is called
- **THEN** it SHALL return a slice of `domain.FundingCredit`

#### Scenario: No active credits
- **WHEN** there are no active credits
- **THEN** it SHALL return an empty slice (not nil)

### Requirement: Error response parsing
The system SHALL parse Bitfinex API error responses and convert them to `domain.AppError`.

#### Scenario: API returns error
- **WHEN** Bitfinex responds with an error (e.g., `["error", 10114, "nonce: small"]`)
- **THEN** the system SHALL parse the error code and message into a descriptive `domain.AppError`
