## ADDED Requirements

### Requirement: Redis client initialization

The system SHALL create a Redis client from the `REDIS_URL` environment variable using `redis.ParseURL`, supporting both `redis://` and `rediss://` (TLS) schemes.

#### Scenario: Successful connection

- **WHEN** the application starts with a valid `REDIS_URL`
- **THEN** the system SHALL create a `*redis.Client` and verify connectivity with PING

#### Scenario: Missing REDIS_URL

- **WHEN** the application starts without `REDIS_URL` set
- **THEN** the system SHALL fail to start with an error indicating the missing variable

### Requirement: Redis lifecycle management

The system SHALL register the Redis client with fx Lifecycle to close the connection on shutdown.

#### Scenario: Graceful shutdown

- **WHEN** the application receives a shutdown signal
- **THEN** the system SHALL close the Redis client connection

### Requirement: Health check includes Redis

The system SHALL include Redis connectivity status in the health check response.

#### Scenario: Redis healthy

- **WHEN** `GET /api/v1/health` is called and Redis is reachable
- **THEN** the response SHALL include `"redis": "ok"`

#### Scenario: Redis unreachable

- **WHEN** `GET /api/v1/health` is called and Redis PING fails
- **THEN** the response SHALL include `"redis": "error"` and the overall status SHALL be `"degraded"`
