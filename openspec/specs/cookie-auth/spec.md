# cookie-auth Specification

## Purpose
TBD - created by archiving change api-proxy-cookie-auth. Update Purpose after archive.
## Requirements
### Requirement: Login Server Action
The login Server Action SHALL call the Go backend `POST /api/v1/auth/login` with email and password, and on success set an HttpOnly cookie named `auth_token` with the returned JWT.

#### Scenario: Successful login
- **WHEN** the login action is called with valid credentials
- **THEN** the action SHALL set `auth_token` cookie with flags: HttpOnly=true, Secure=production, SameSite=Lax, Path=/, Max-Age=604800
- **AND** return a success result

#### Scenario: Failed login
- **WHEN** the backend returns a non-2xx response (invalid credentials)
- **THEN** the action SHALL return an error result with the backend's error message
- **AND** SHALL NOT set any cookie

### Requirement: Register Server Action
The register Server Action SHALL call `POST /api/v1/auth/register`, and on success automatically call the login endpoint to set the auth cookie.

#### Scenario: Successful registration
- **WHEN** the register action is called with valid email and password
- **THEN** the action SHALL create the account AND set `auth_token` cookie (auto-login)

#### Scenario: Failed registration
- **WHEN** the backend returns a non-2xx response (e.g. duplicate email)
- **THEN** the action SHALL return an error result without setting a cookie

### Requirement: Logout Server Action
The logout Server Action SHALL delete the `auth_token` cookie.

#### Scenario: Logout
- **WHEN** the logout action is called
- **THEN** the `auth_token` cookie SHALL be deleted
- **AND** the action SHALL redirect to the login page

### Requirement: Cookie security flags
All cookie operations SHALL use the security flags defined in the architecture document.

#### Scenario: Production environment
- **WHEN** the app runs in production (`NODE_ENV === "production"`)
- **THEN** the `Secure` flag SHALL be `true`

#### Scenario: Development environment
- **WHEN** the app runs in development
- **THEN** the `Secure` flag SHALL be `false` (allow HTTP on localhost)

