## ADDED Requirements

### Requirement: Lending engine health status
The system SHALL expose the lending engine's operational status through the existing health check endpoint, providing visibility into engine running state, active worker count, and circuit breaker status.

#### Scenario: Health check includes engine status when running
- **WHEN** GET /api/v1/health is called and the lending engine is running
- **THEN** the response includes an `engine` object with `running: true`, `worker_count`, and `circuit_breaker` status

#### Scenario: Health check includes engine status when stopped
- **WHEN** GET /api/v1/health is called and the lending engine has not started
- **THEN** the response includes an `engine` object with `running: false`

#### Scenario: Engine health does not affect overall status
- **WHEN** the lending engine is unhealthy but DB and Redis are healthy
- **THEN** the overall health status remains "healthy" (engine is non-critical for API availability)
