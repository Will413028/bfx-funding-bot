## ADDED Requirements

### Requirement: Dual cookie management
After login, the frontend SHALL set two httpOnly cookies:
- `auth_token`: access token, httpOnly, secure, sameSite=lax, maxAge=15 minutes
- `refresh_token`: refresh token, httpOnly, secure, sameSite=strict, path=/api, maxAge=7 days

#### Scenario: Login sets both cookies
- **WHEN** login succeeds
- **THEN** both `auth_token` and `refresh_token` cookies SHALL be set with correct attributes

#### Scenario: Logout clears both cookies
- **WHEN** user logs out
- **THEN** both `auth_token` and `refresh_token` cookies SHALL be deleted

### Requirement: Transparent token refresh in API proxy
The API proxy (`/api/proxy/[...path]`) SHALL detect 401 responses from the backend, attempt a refresh using the `refresh_token` cookie, and retry the original request with the new access token.

#### Scenario: Expired access token triggers automatic refresh
- **WHEN** the backend returns 401 for a proxied request AND a `refresh_token` cookie exists
- **THEN** the proxy SHALL call `POST /api/v1/auth/refresh` with the refresh token
- **AND** set new `auth_token` and `refresh_token` cookies from the response
- **AND** retry the original request with the new access token

#### Scenario: Refresh also fails — redirect to login
- **WHEN** the refresh attempt also fails (401)
- **THEN** the proxy SHALL clear both cookies and return 401 to the client

#### Scenario: No refresh token available — pass through 401
- **WHEN** the backend returns 401 AND no `refresh_token` cookie exists
- **THEN** the proxy SHALL return 401 to the client without attempting refresh

### Requirement: Middleware handles short-lived access token
The frontend middleware SHALL gracefully handle expired access tokens by allowing the request to proceed (the proxy will handle refresh). It SHALL only redirect to login if neither cookie exists.

#### Scenario: Expired access token but refresh token present
- **WHEN** `auth_token` is expired but `refresh_token` cookie exists
- **THEN** middleware SHALL allow the request to proceed (proxy handles refresh)

#### Scenario: No cookies at all
- **WHEN** neither `auth_token` nor `refresh_token` cookie exists
- **THEN** middleware SHALL redirect to login for protected routes
