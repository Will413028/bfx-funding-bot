### Requirement: Health check endpoint
The system SHALL expose a `GET /api/v1/health` endpoint that returns the server's health status, including database connectivity.

#### Scenario: Server and database are healthy
- **WHEN** a GET request is made to `/api/v1/health` and the database is reachable
- **THEN** the response SHALL be status 200 with `{ "status": "ok" }`

#### Scenario: Database is unreachable
- **WHEN** a GET request is made to `/api/v1/health` and the database ping fails
- **THEN** the response SHALL be status 503 with `{ "status": "unhealthy", "error": "database unreachable" }`

### Requirement: Health check does not require authentication
The health check endpoint SHALL be accessible without any authentication.

#### Scenario: Unauthenticated health check
- **WHEN** a GET request is made to `/api/v1/health` without any auth headers or cookies
- **THEN** the response SHALL be status 200 with `{ "status": "ok" }`

### Requirement: Koyeb readiness probe compatibility
The health check response SHALL return within 5 seconds and use standard HTTP status codes (200 for healthy) compatible with Koyeb's health check configuration.

#### Scenario: Koyeb polls health endpoint
- **WHEN** Koyeb sends a health check request to `/api/v1/health`
- **THEN** the response SHALL return status 200 within 5 seconds
