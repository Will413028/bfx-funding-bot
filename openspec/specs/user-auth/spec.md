## ADDED Requirements

### Requirement: User registration
The system SHALL allow new users to register with an email address and password. The password SHALL be stored as a bcrypt hash (cost=12), never in plaintext. The email SHALL be unique across all users. Upon successful registration, the system SHALL return the created user's ID and email.

#### Scenario: Successful registration
- **WHEN** a POST request is sent to `/api/v1/auth/register` with a valid email and password (min 8 characters)
- **THEN** the system creates a new user record with status "active", returns HTTP 201 with `{ "id": "<uuid>", "email": "<email>" }`

#### Scenario: Duplicate email
- **WHEN** a POST request is sent to `/api/v1/auth/register` with an email that already exists
- **THEN** the system returns HTTP 409 with error code `EMAIL_ALREADY_EXISTS`

#### Scenario: Invalid email format
- **WHEN** a POST request is sent to `/api/v1/auth/register` with an invalid email format
- **THEN** the system returns HTTP 400 with error code `INVALID_EMAIL`

#### Scenario: Password too short
- **WHEN** a POST request is sent to `/api/v1/auth/register` with a password shorter than 8 characters
- **THEN** the system returns HTTP 400 with error code `PASSWORD_TOO_SHORT`

### Requirement: User login
The system SHALL authenticate users by email and password, and return a JWT access token (RS256) upon successful authentication. The token SHALL contain the user's ID and email in the payload.

#### Scenario: Successful login
- **WHEN** a POST request is sent to `/api/v1/auth/login` with a valid email and correct password
- **THEN** the system returns HTTP 200 with `{ "token": "<jwt>", "expires_at": "<iso8601>" }`

#### Scenario: Wrong password
- **WHEN** a POST request is sent to `/api/v1/auth/login` with a valid email but incorrect password
- **THEN** the system returns HTTP 401 with error code `INVALID_CREDENTIALS`

#### Scenario: Non-existent user
- **WHEN** a POST request is sent to `/api/v1/auth/login` with an email that does not exist
- **THEN** the system returns HTTP 401 with error code `INVALID_CREDENTIALS` (same as wrong password to prevent user enumeration)

#### Scenario: Suspended user login
- **WHEN** a POST request is sent to `/api/v1/auth/login` with credentials of a suspended user
- **THEN** the system returns HTTP 403 with error code `USER_SUSPENDED`

### Requirement: JWT token structure
The JWT access token SHALL use RS256 signing algorithm with a configurable key pair. The token SHALL expire after 24 hours. The token payload SHALL include `sub` (user ID), `email`, `iat` (issued at), and `exp` (expiration).

#### Scenario: Token contains required claims
- **WHEN** a JWT token is decoded
- **THEN** the payload contains `sub` (UUID), `email` (string), `iat` (unix timestamp), and `exp` (unix timestamp = iat + 86400)

### Requirement: JWT authentication middleware
The system SHALL protect all API routes under `/api/v1/*` (except `/api/v1/auth/*` and `/api/v1/health`) with JWT verification. The middleware SHALL extract the token from the `Authorization: Bearer <token>` header.

#### Scenario: Valid token grants access
- **WHEN** a request is sent to a protected endpoint with a valid, non-expired JWT in the Authorization header
- **THEN** the request proceeds to the handler with user ID and email available in the request context

#### Scenario: Missing Authorization header
- **WHEN** a request is sent to a protected endpoint without an Authorization header
- **THEN** the system returns HTTP 401 with error code `MISSING_TOKEN`

#### Scenario: Invalid token
- **WHEN** a request is sent to a protected endpoint with a malformed or tampered JWT
- **THEN** the system returns HTTP 401 with error code `INVALID_TOKEN`

#### Scenario: Expired token
- **WHEN** a request is sent to a protected endpoint with an expired JWT
- **THEN** the system returns HTTP 401 with error code `TOKEN_EXPIRED`

#### Scenario: Public routes bypass auth
- **WHEN** a request is sent to `/api/v1/auth/register`, `/api/v1/auth/login`, or `/api/v1/health`
- **THEN** the request proceeds without JWT verification

### Requirement: Unified error response format
All authentication errors SHALL follow the project's unified error format: `{ "error": { "code": "<ERROR_CODE>", "message": "<human readable>" } }`.

#### Scenario: Auth error format
- **WHEN** any authentication-related error occurs (registration, login, or middleware)
- **THEN** the response body matches `{ "error": { "code": "...", "message": "..." } }` with appropriate HTTP status code
