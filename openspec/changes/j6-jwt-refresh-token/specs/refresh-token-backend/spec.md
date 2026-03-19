## ADDED Requirements

### Requirement: Access token short expiry
The access token (JWT) expiry SHALL be reduced from 24 hours to 15 minutes. The token format (RS256, Claims struct) SHALL remain unchanged.

#### Scenario: Access token expires after 15 minutes
- **WHEN** a user logs in and receives an access token
- **THEN** the token's `exp` claim SHALL be set to 15 minutes from issuance

### Requirement: Refresh token generation on login
The login endpoint SHALL generate both an access token and a refresh token. The refresh token SHALL be a 64-byte cryptographically random opaque string. The SHA-256 hash of the refresh token SHALL be stored in Redis with the user's ID and a TTL of 7 days.

#### Scenario: Successful login returns both tokens
- **WHEN** a user logs in with valid credentials
- **THEN** the response SHALL include `accessToken`, `refreshToken`, and `expiresAt`

#### Scenario: Refresh token stored in Redis
- **WHEN** a refresh token is generated
- **THEN** its SHA-256 hash SHALL be stored in Redis with tokenType="refresh" and TTL=7 days

### Requirement: Refresh endpoint with token rotation
The system SHALL provide a `POST /api/v1/auth/refresh` endpoint. It SHALL accept a refresh token, validate it against Redis, generate a new access + refresh token pair, invalidate the old refresh token, and return the new pair.

#### Scenario: Valid refresh token returns new token pair
- **WHEN** a valid refresh token is submitted to the refresh endpoint
- **THEN** a new access token and new refresh token SHALL be returned
- **AND** the old refresh token SHALL be deleted from Redis

#### Scenario: Invalid or expired refresh token rejected
- **WHEN** an invalid or expired refresh token is submitted
- **THEN** the endpoint SHALL return 401 INVALID_TOKEN

#### Scenario: Used refresh token rejected (rotation)
- **WHEN** a refresh token that has already been rotated is submitted again
- **THEN** the endpoint SHALL return 401 INVALID_TOKEN

### Requirement: Logout revokes refresh token
The system SHALL provide a `POST /api/v1/auth/logout` endpoint (protected, JWT required). It SHALL delete the user's refresh token from Redis, making it immediately unusable.

#### Scenario: Logout deletes refresh token
- **WHEN** an authenticated user calls the logout endpoint
- **THEN** the refresh token associated with that user SHALL be deleted from Redis

### Requirement: Login rejects pending users before generating tokens
Login SHALL continue to reject users with status "pending" (EMAIL_NOT_VERIFIED) before generating any tokens.

#### Scenario: Pending user cannot login
- **WHEN** a user with status "pending" attempts to login
- **THEN** no tokens SHALL be generated and EMAIL_NOT_VERIFIED error SHALL be returned
