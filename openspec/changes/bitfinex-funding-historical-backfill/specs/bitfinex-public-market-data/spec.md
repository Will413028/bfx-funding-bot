## ADDED Requirements

### Requirement: Bitfinex client SHALL fetch funding candles via public REST endpoint

The bitfinex client SHALL provide a `GetFundingCandles` method that calls `/v2/candles/trade:{timeframe}:f{currency}:{period_or_aggr}/hist` and returns parsed OHLCV records, supporting `start`, `end`, and `limit` parameters.

#### Scenario: Fetching recent fUST 1h aggregated candles

- **WHEN** the method is invoked with `currency='UST', timeframe='1h', periodOrAggr='a30:p2:p30', limit=5, start=0, end=0`
- **THEN** the method returns up to 5 candle records ordered newest-first, each containing MTS, OPEN, CLOSE, HIGH, LOW, VOLUME fields

#### Scenario: Fetching candles within a time window

- **WHEN** the method is invoked with explicit `start` and `end` millisecond timestamps
- **THEN** the method returns only candles with MTS within `[start, end]` (Bitfinex inclusive semantics)

#### Scenario: Empty response for unsupported symbol

- **WHEN** the method is invoked with `currency='USDT'` (a symbol Bitfinex does not list as `fUSDT`)
- **THEN** the method returns an empty slice (not an error)

### Requirement: Bitfinex client SHALL fetch funding statistics via public REST endpoint

The bitfinex client SHALL provide a `GetFundingStats` method that calls `/v2/funding/stats/f{currency}/hist` and returns parsed records containing FRR, average period, funding amount, funding amount used, and funding below threshold.

#### Scenario: Fetching recent fUST funding stats

- **WHEN** the method is invoked with `currency='UST', limit=3, start=0, end=0`
- **THEN** the method returns up to 3 stats records, each containing MTS, FRR, AvgPeriod, FundingAmount, FundingAmountUsed, FundingBelowThreshold (the four-null padding fields in the raw response are skipped)

#### Scenario: Fetching stats within a time window

- **WHEN** the method is invoked with explicit `start` and `end` millisecond timestamps
- **THEN** only records within the requested range are returned

### Requirement: Public market data calls SHALL respect Bitfinex rate limits

The client SHALL apply the existing token-bucket rate limiter to public funding endpoints so that no more than 30 requests per minute are issued, matching Bitfinex's documented limit for `/v2/candles/...`.

#### Scenario: Burst rate-limit clamping

- **WHEN** the client is configured with `WithRateLimit(0.5, 1)` and 5 sequential `GetFundingCandles` calls are made without sleep
- **THEN** the total elapsed wall-clock time is at least 8 seconds (4 inter-call waits × 2 seconds) confirming the limiter is active

### Requirement: Client SHALL retry rate-limit errors with exponential backoff

When the Bitfinex API returns HTTP 429 or a Bitfinex error indicating rate limiting (error code 11010 or response body containing `"ratelimit:"`), the client SHALL automatically wait and retry with exponential backoff (60s → 120s → 240s → 240s, cap 4 attempts) before surfacing the error to the caller.

#### Scenario: Single rate-limit response triggers backoff and retry

- **WHEN** the upstream API responds with HTTP 429 and body `["error", 11010, "ratelimit: request denied"]` on the first call but succeeds on the second
- **THEN** the client returns the successful response and the elapsed wall-clock time is at least the configured first backoff base (default 60s; can be overridden via `WithRateLimitBackoffBase` for tests)

#### Scenario: All retry attempts exhausted

- **WHEN** the upstream API responds with rate-limit error on 5 consecutive calls (initial + 4 retries)
- **THEN** the client surfaces an error containing the upstream body and the call count, allowing the caller to log and abort

#### Scenario: Context cancellation during backoff

- **WHEN** the caller's context is cancelled while the client is sleeping between retries
- **THEN** the client returns `ctx.Err()` immediately without completing the remaining wait

### Requirement: Client SHALL allow rate-limit backoff base to be configured

The client SHALL expose a `WithRateLimitBackoffBase(d time.Duration)` ClientOption that sets the first backoff interval (default 60s). Subsequent intervals double up to a cap of 4× the base.

#### Scenario: Test override of backoff base

- **WHEN** the client is constructed with `WithRateLimitBackoffBase(50 * time.Millisecond)` and a single 429 → 200 sequence is mocked
- **THEN** the retry happens within ~100ms, not 60+ seconds, allowing fast unit tests

### Requirement: Public market data calls SHALL not require authentication credentials

`GetFundingCandles` and `GetFundingStats` SHALL be callable without API key / secret arguments, distinguishing them from authenticated endpoints (`SubmitFundingOffer`, `GetFundingBalance`, etc.).

#### Scenario: Method signature excludes credentials

- **WHEN** code reads the function signatures of `GetFundingCandles` and `GetFundingStats` via Go reflection / source
- **THEN** the signatures take only `ctx context.Context` and domain parameters, with no `apiKey` or `apiSecret` parameters
