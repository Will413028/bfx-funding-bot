## MODIFIED Requirements

### Requirement: Health check endpoint
The system SHALL expose a `GET /api/v1/health` endpoint that returns the server's health status, including database connectivity.

#### Scenario: Server and database are healthy
- **WHEN** a GET request is made to `/api/v1/health` and the database is reachable
- **THEN** the response SHALL be status 200 with `{ "status": "ok" }`

#### Scenario: Database is unreachable
- **WHEN** a GET request is made to `/api/v1/health` and the database ping fails
- **THEN** the response SHALL be status 503 with `{ "status": "unhealthy", "error": "database unreachable" }`
