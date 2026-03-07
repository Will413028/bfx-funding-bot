## ADDED Requirements

### Requirement: HTTP server starts on configured port
The system SHALL start a Gin HTTP server on the port specified by the `PORT` environment variable, defaulting to `8080` if not set.

#### Scenario: Server starts with default port
- **WHEN** `PORT` environment variable is not set
- **THEN** the server SHALL listen on port `8080`

#### Scenario: Server starts with custom port
- **WHEN** `PORT` environment variable is set to `3000`
- **THEN** the server SHALL listen on port `3000`

### Requirement: Graceful shutdown on SIGTERM
The system SHALL perform graceful shutdown when receiving SIGTERM signal, completing in-flight requests before terminating.

#### Scenario: SIGTERM received with active requests
- **WHEN** the server receives SIGTERM while handling requests
- **THEN** the server SHALL stop accepting new connections, wait for in-flight requests to complete (up to 10 seconds), and then shut down

#### Scenario: SIGTERM received with no active requests
- **WHEN** the server receives SIGTERM with no active requests
- **THEN** the server SHALL shut down immediately

### Requirement: fx manages component lifecycle
The system SHALL use go.uber.org/fx to wire all dependencies and manage start/stop lifecycle of the HTTP server.

#### Scenario: Application startup
- **WHEN** the application starts
- **THEN** fx SHALL resolve all dependencies, start the HTTP server, and log the listening address

#### Scenario: Application shutdown
- **WHEN** fx receives a shutdown signal
- **THEN** fx SHALL stop components in LIFO order (HTTP server first, then other components)

### Requirement: CORS middleware allows frontend origin
The system SHALL include CORS middleware that allows requests from the configured frontend URL.

#### Scenario: Request from allowed origin
- **WHEN** a request arrives with `Origin: https://app.example.com` and `FRONTEND_URL` is set to `https://app.example.com`
- **THEN** the response SHALL include `Access-Control-Allow-Origin: https://app.example.com` and `Access-Control-Allow-Credentials: true`

#### Scenario: Preflight request
- **WHEN** an OPTIONS preflight request arrives from the allowed origin
- **THEN** the response SHALL include appropriate `Access-Control-Allow-Methods` and `Access-Control-Allow-Headers` headers with status 204

#### Scenario: Request from disallowed origin
- **WHEN** a request arrives with an origin not matching `FRONTEND_URL`
- **THEN** the response SHALL NOT include `Access-Control-Allow-Origin` header

### Requirement: Request ID middleware
The system SHALL assign a unique request ID to every incoming request and include it in the response headers.

#### Scenario: Request without existing ID
- **WHEN** a request arrives without `X-Request-ID` header
- **THEN** the server SHALL generate a UUID, set it in the request context, and return it in the `X-Request-ID` response header

#### Scenario: Request with existing ID
- **WHEN** a request arrives with `X-Request-ID: abc-123`
- **THEN** the server SHALL use `abc-123` as the request ID and return it in the `X-Request-ID` response header

### Requirement: Structured logging with zap
The system SHALL use `go.uber.org/zap` for all logging, outputting JSON format with request context. fx SHALL be configured with `fx.WithLogger` to use zap for framework-level logs.

#### Scenario: Request logging
- **WHEN** a request is processed
- **THEN** the log entry SHALL include `method`, `path`, `status`, `duration`, and `request_id` fields in JSON format

#### Scenario: fx lifecycle logging
- **WHEN** fx starts or stops components
- **THEN** fx SHALL log lifecycle events via zap

### Requirement: Versioned API route group
All API endpoints SHALL be registered under the `/api/v1` route group.

#### Scenario: Route registration
- **WHEN** the router is initialized
- **THEN** all endpoints SHALL be accessible under `/api/v1/*` prefix
