## ADDED Requirements

### Requirement: WS token generation endpoint
The system SHALL provide a `POST /api/v1/auth/ws-token` endpoint (JWT-protected) that returns a short-lived, single-use token for WebSocket authentication.

#### Scenario: Successful token generation
- **WHEN** an authenticated user calls `POST /api/v1/auth/ws-token`
- **THEN** the server SHALL return a JSON response `{"token":"<hex-string>"}` with a 30-second TTL

#### Scenario: Unauthenticated request
- **WHEN** an unauthenticated user calls `POST /api/v1/auth/ws-token`
- **THEN** the server SHALL respond with HTTP 401

### Requirement: WS token validation
The system SHALL validate a ws-token by looking it up in Redis and extracting the associated user ID. The token SHALL be consumed (deleted) upon successful validation.

#### Scenario: Valid token
- **WHEN** a ws-token is validated
- **THEN** the system SHALL return the associated user ID and delete the token from Redis

#### Scenario: Expired token
- **WHEN** a ws-token has expired (past 30-second TTL)
- **THEN** Redis auto-expires the key and validation SHALL fail

#### Scenario: Reused token
- **WHEN** a ws-token that has already been consumed is used again
- **THEN** validation SHALL fail (key no longer exists in Redis)

### Requirement: WS token format
The ws-token SHALL be a cryptographically random 32-byte value encoded as a 64-character hexadecimal string, stored in Redis with key `ws:token:<hex>` and value `<user_id>`.

#### Scenario: Token uniqueness
- **WHEN** a new ws-token is generated
- **THEN** it SHALL be generated using `crypto/rand` to ensure uniqueness
