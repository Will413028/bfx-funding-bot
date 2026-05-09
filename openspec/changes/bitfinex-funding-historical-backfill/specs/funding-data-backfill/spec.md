## ADDED Requirements

### Requirement: Backfill runner SHALL be invokable as a standalone command

The system SHALL provide an executable at `cmd/backfill/main.go` that can be run via `go run ./cmd/backfill` or as a compiled binary, accepting CLI flags `-currencies` (CSV, e.g. `UST,USD`), `-timeframes` (CSV, e.g. `1h,1D`), `-period-aggr` (default `a30:p2:p30`), `-start` (RFC3339 date), `-end` (RFC3339 date, default now), `-batch-size` (default 10000), `-also-stats` (default true), and `-verify` (default false).

#### Scenario: Help flag prints usage

- **WHEN** the binary is invoked with `-h` or `--help`
- **THEN** stderr / stdout contains the flag list with descriptions and exit code is 0

#### Scenario: Missing required flag

- **WHEN** the binary is invoked without `-currencies`
- **THEN** exit code is 2 and stderr contains a clear error message naming the missing flag

#### Scenario: Multiple currencies and timeframes form a backfill matrix

- **WHEN** the binary is invoked with `-currencies UST,USD -timeframes 1h,1D`
- **THEN** the runner processes 4 (currency, timeframe) combinations sequentially: (UST, 1h), (UST, 1D), (USD, 1h), (USD, 1D)

### Requirement: Backfill runner SHALL fetch and persist funding candles

The runner SHALL invoke the Bitfinex client's `GetFundingCandles` method and persist the result into the `funding_candles` table using `INSERT ... ON CONFLICT DO NOTHING` semantics.

#### Scenario: First-run backfill writes candles

- **WHEN** the runner is invoked against an empty `funding_candles` table with `-currency UST -timeframe 1h -period-aggr a30:p2:p30 -start 2026-05-01 -end 2026-05-09`
- **THEN** after completion `SELECT count(*) FROM funding_candles WHERE symbol='fUST' AND timeframe='1h'` returns ≥ 100 rows (~24 candles per day × 8 days, allowing for weekend gaps)

#### Scenario: Re-run is idempotent

- **WHEN** the same backfill command is executed twice with no DB changes between runs
- **THEN** the row count after the second run equals the row count after the first run, and no SQL constraint errors are emitted

### Requirement: Backfill runner SHALL also persist funding statistics

For each currency the runner is invoked with, it SHALL also fetch from `/v2/funding/stats/...` and persist into `funding_stats` for the same time range.

#### Scenario: Stats backfill alongside candles

- **WHEN** the runner completes a candles backfill for `fUST` over a given range
- **THEN** the runner also persists at least one `funding_stats` row for the same `symbol='fUST'` whose `mts` falls within the same range

### Requirement: Backfill runner SHALL respect rate limits and report progress

The runner SHALL use the bitfinex client's built-in rate limiter (no manual `time.Sleep`) and SHALL log progress at minimum once per HTTP request showing the request range and result row count.

#### Scenario: Progress logging during multi-page backfill

- **WHEN** the runner makes 3 sequential paged requests to fetch a 3-year candle range
- **THEN** stdout / structured log contains 3 lines (or 3 events) each reporting the request `start_mts`, `end_mts`, and `rows_received` for that page

#### Scenario: Rate-limit compliance

- **WHEN** the runner is invoked over a range requiring 5 paged requests
- **THEN** total elapsed wall time is ≥ 8 seconds, demonstrating the limiter throttled the request rate

### Requirement: Backfill runner SHALL handle empty / out-of-range responses

When Bitfinex returns an empty response (e.g., requested range pre-dates symbol availability), the runner SHALL stop pagination for that (currency, timeframe) combination and continue with the next combination, exiting code 0 overall.

#### Scenario: Range earlier than fUST history start

- **WHEN** the runner is invoked with `-currencies UST -timeframes 1h -start 2017-01-01 -end 2018-06-01`
- **THEN** the runner exits with code 0, logs that the range yielded zero rows for `(fUST, 1h)`, and writes no rows to either table

### Requirement: Pagination cursor logic SHALL be testable as a pure function

The backward-pagination loop SHALL be implemented as a standalone function `paginateBackward(ctx, fetch, startMs, endMs, batchSize) error` accepting a `fetch` callback so it can be unit-tested without HTTP or DB dependencies.

#### Scenario: Empty fetch response stops loop

- **WHEN** `paginateBackward` is invoked with a `fetch` that returns an empty slice on the first call
- **THEN** the function returns nil after exactly one fetch invocation

#### Scenario: Cursor reaches start boundary

- **WHEN** `paginateBackward` is invoked with `startMs=1000, endMs=10000` and `fetch` returns `[5000, 4000, 3000]` then `[2500, 1500, 999]` then would return more
- **THEN** the function returns after the second batch (because earliest 999 ≤ startMs 1000), without making a third fetch

### Requirement: Backfill runner SHALL support `--verify` mode

When invoked with `--verify`, the runner SHALL after completing all backfill execute two verification steps:
1. **Gap detection** for each `(currency, timeframe)` combination, listing time gaps that exceed the expected interval × 1.5
2. **FRR unit verification** sampling 5 pairs of `(funding_stats.frr, same-period funding_candles.close)` and reporting the ratio so the operator can determine whether FRR is per-second / per-day / per-period

#### Scenario: Gap detection on backfilled data

- **WHEN** `--verify` is invoked after a successful 3-year backfill with no missing periods
- **THEN** the verify output contains a line like "fUST 1h: 0 large gaps detected" for each (currency, timeframe) combo

#### Scenario: FRR ratio analysis

- **WHEN** `--verify` is invoked and at least one (stat, candle) sample pair exists at the same MTS (within ±1 hour)
- **THEN** the verify output contains a line like "fUST FRR/candle ratio = 169.2x → likely per-second (86400/512=169)" for each currency

#### Scenario: Verify mode with empty database

- **WHEN** `--verify` is invoked when both `funding_candles` and `funding_stats` are empty
- **THEN** the runner exits 0 with output "nothing to verify, run backfill first"
