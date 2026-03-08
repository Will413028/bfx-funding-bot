### Requirement: Rate limit by client IP on public routes

The system SHALL limit requests from each client IP to the public auth routes (`/api/v1/auth/*`) at a rate of 5 requests per second with a burst allowance of 10.

#### Scenario: Request within limit

- **WHEN** a client sends a request to `POST /api/v1/auth/login` and has not exceeded the rate limit
- **THEN** the request SHALL be processed normally and the response SHALL include `X-RateLimit-Limit` and `X-RateLimit-Remaining` headers

#### Scenario: Request exceeds limit

- **WHEN** a client IP sends more than 10 requests in rapid succession to `POST /api/v1/auth/register`
- **THEN** the system SHALL respond with HTTP 429 Too Many Requests, a JSON body `{"error": {"code": "RATE_LIMITED", "message": "Too many requests"}}`, and a `Retry-After` header indicating seconds to wait

### Requirement: Rate limit by client IP on protected routes

The system SHALL limit requests from each client IP to protected routes (all JWT-authenticated endpoints) at a rate of 20 requests per second with a burst allowance of 40.

#### Scenario: Protected route within limit

- **WHEN** an authenticated client sends a request to `GET /api/v1/dashboard` and has not exceeded the rate limit
- **THEN** the request SHALL be processed normally with rate limit headers included

#### Scenario: Protected route exceeds limit

- **WHEN** an authenticated client IP sends more than 40 requests in rapid succession to protected endpoints
- **THEN** the system SHALL respond with HTTP 429 Too Many Requests with the standard error format

### Requirement: Health check exempt from rate limiting

The system SHALL NOT apply rate limiting to the `GET /api/v1/health` endpoint.

#### Scenario: Health check always succeeds

- **WHEN** a client sends 100 rapid requests to `GET /api/v1/health`
- **THEN** all requests SHALL be processed normally regardless of rate limit state

### Requirement: Rate limit response headers

The system SHALL include rate limit information in response headers for all rate-limited routes.

#### Scenario: Headers present on successful request

- **WHEN** a request is processed within the rate limit
- **THEN** the response SHALL include `X-RateLimit-Limit` (max requests per second) and `X-RateLimit-Remaining` (remaining tokens as integer)

#### Scenario: Retry-After header on 429

- **WHEN** a request is rejected due to rate limiting
- **THEN** the response SHALL include a `Retry-After` header with the number of seconds (integer, rounded up) until the client can retry

### Requirement: Limiter cleanup for idle IPs

The system SHALL periodically clean up rate limiter state for client IPs that have not sent requests for more than 10 minutes, to prevent memory leaks.

#### Scenario: Idle IP cleanup

- **WHEN** a client IP has not sent any request for more than 10 minutes
- **THEN** the system SHALL remove its rate limiter from memory on the next cleanup cycle
