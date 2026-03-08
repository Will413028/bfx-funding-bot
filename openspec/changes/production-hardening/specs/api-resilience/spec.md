## ADDED Requirements

### Requirement: Bitfinex REST API circuit breaker
The system SHALL wrap all Bitfinex REST API calls with a circuit breaker that transitions to OPEN state after consecutive failures, preventing cascading failure across all Workers.

#### Scenario: Circuit breaker opens after consecutive failures
- **WHEN** 5 consecutive Bitfinex REST API calls fail
- **THEN** the circuit breaker transitions to OPEN state and all subsequent calls fail immediately with a circuit-open error for 15 seconds

#### Scenario: Circuit breaker half-open probe
- **WHEN** the circuit breaker is OPEN and 15 seconds have elapsed
- **THEN** the circuit breaker transitions to HALF-OPEN and allows up to 3 probe requests to pass through

#### Scenario: Circuit breaker recovers
- **WHEN** the circuit breaker is in HALF-OPEN state and a probe request succeeds
- **THEN** the circuit breaker transitions back to CLOSED state and normal operation resumes

#### Scenario: Circuit breaker failure count resets
- **WHEN** the circuit breaker is CLOSED and 60 seconds pass without reaching the failure threshold
- **THEN** the failure counter resets to zero

### Requirement: Bitfinex REST API rate limiter
The system SHALL enforce a global rate limit on all outgoing Bitfinex REST API calls to stay within the exchange's rate limit (90 req/min), using a token bucket at 1.0 req/s (60 req/min with 30% buffer).

#### Scenario: Rate limiter throttles excess requests
- **WHEN** multiple Workers submit Bitfinex API requests exceeding 1.0 req/s
- **THEN** excess requests block (wait) until a token is available, respecting the caller's context cancellation

#### Scenario: Rate limiter respects context cancellation
- **WHEN** a rate-limited request is waiting for a token and the context is cancelled
- **THEN** the request returns immediately with a context error

### Requirement: Deterministic engine startup
The system SHALL replace the `time.Sleep(50ms)` in `startLendingEngine` with a deterministic synchronization mechanism that waits for `lending.Service` to complete initialization.

#### Scenario: Engine startup with ready signal
- **WHEN** `startLendingEngine` starts the lending service
- **THEN** it waits for a ready signal (channel close) before proceeding to resume workers, with a 5-second timeout

#### Scenario: Engine startup timeout
- **WHEN** `lending.Service.Start()` does not signal readiness within 5 seconds
- **THEN** `startLendingEngine` logs a warning and proceeds anyway (best-effort)

### Requirement: Context propagation to lending engine
The system SHALL propagate `context.Context` from HTTP handlers through service layer into lending engine operations (StartWorker, StopWorker, ReloadConfig).

#### Scenario: Request ID flows to lending engine
- **WHEN** an HTTP request with `X-Request-ID` triggers StartWorker via APIKeyService.Create
- **THEN** the context passed to `lending.Service.StartWorker` contains the request ID for structured logging
