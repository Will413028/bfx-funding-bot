## MODIFIED Requirements

### Requirement: Rate limit by client IP on public routes

The system SHALL limit requests from each client IP to the public auth routes (`/api/v1/auth/*`) at a rate of 5 requests per second with a burst allowance of 10.

#### Scenario: Request within limit

- **WHEN** a client sends a request to `POST /api/v1/auth/login` and has not exceeded the rate limit
- **THEN** the request SHALL be processed normally and the response SHALL include `X-RateLimit-Limit` and `X-RateLimit-Remaining` headers

#### Scenario: Request exceeds limit

- **WHEN** a client IP sends more than 10 requests in rapid succession to `POST /api/v1/auth/register`
- **THEN** the system SHALL respond with HTTP 429 Too Many Requests, a JSON body `{"error": {"code": "RATE_LIMITED", "message": "Too many requests"}}`, and a `Retry-After` header indicating seconds to wait

## ADDED Requirements

### Requirement: Platform-level Bitfinex API rate limiter

The `bitfinex.Client` SHALL use a platform-level `rate.Limiter` as a safety net to prevent exceeding the Bitfinex API platform-wide rate limit. The rate and burst SHALL be configurable via `ClientOption` functional options, with defaults of 15 req/s and burst 20.

#### Scenario: Platform limiter does not throttle under normal load

- **WHEN** the system has fewer than 15 concurrent Bitfinex API requests per second across all users
- **THEN** the platform-level limiter SHALL not block any requests

#### Scenario: Platform limiter throttles under extreme load

- **WHEN** the system exceeds 20 concurrent Bitfinex API requests in a burst
- **THEN** the platform-level limiter SHALL block excess requests until tokens are available

#### Scenario: Custom platform rate via option

- **WHEN** `bitfinex.NewClient(httpClient, WithRateLimit(30, 40))` is called
- **THEN** the client's platform limiter SHALL use rate=30 and burst=40
