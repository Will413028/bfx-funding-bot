## 1. Schema & Migration ✓ DONE 2026-05-09

- [x] 1.1 Edit `backend/schema/schema.hcl` adding `funding_candles` table block: columns (`symbol text NOT NULL`, `timeframe text NOT NULL`, `period_agg text NOT NULL`, `mts bigint NOT NULL`, `open double_precision`, `close double_precision`, `high double_precision`, `low double_precision`, `volume double_precision`), composite PK on `(symbol, timeframe, period_agg, mts)`, index on `mts`
- [x] 1.2 Edit `backend/schema/schema.hcl` adding `funding_stats` table block: columns (`symbol text NOT NULL`, `mts bigint NOT NULL`, `frr double_precision`, `avg_period double_precision`, `funding_amount double_precision`, `funding_amount_used double_precision`, `funding_below_threshold double_precision`), composite PK on `(symbol, mts)`, index on `mts`
- [x] 1.3 `cd backend/schema && atlas migrate diff add_funding_history_tables --env neon` and human-review the generated SQL in `backend/migrations/20260509100512_add_funding_history_tables.sql`
- [x] 1.4 `cd backend/schema && source ../../.env && atlas migrate apply --env neon` to apply migration to Neon

## 2. Bitfinex Client — Public Endpoints + Rate-Limit Backoff

- [ ] 2.1 Add `domain.FundingCandle` struct in `backend/internal/domain/funding_market_data.go` with fields Symbol/Timeframe/PeriodAgg (string), MTS (int64), Open/Close/High/Low/Volume (float64)
- [ ] 2.2 Add `domain.FundingStat` struct in same file with fields Symbol (string), MTS (int64), FRR/AvgPeriod/FundingAmount/FundingAmountUsed/FundingBelowThreshold (float64)
- [ ] 2.3 Add `WithRateLimitBackoffBase(d time.Duration) ClientOption` in `backend/internal/bitfinex/client.go` (default 60s; stored on `*Client`)
- [ ] 2.4 Add `isRateLimitError(status int, body []byte) bool` helper in `client.go` detecting HTTP 429 OR body containing `"ratelimit:"` OR body containing `11010`
- [ ] 2.5 Modify `(c *Client) doHTTP(...)` in `client.go` to wrap the existing send loop with rate-limit-aware retry: on `isRateLimitError` true, sleep for `min(backoffBase << attempt, 4 * backoffBase)` (cap 4 attempts), respect `ctx.Done()`, log warning with attempt + wait
- [ ] 2.6 Implement `(c *Client) GetFundingCandles(ctx, currency, timeframe, periodOrAggr string, start, end int64, limit int) ([]domain.FundingCandle, error)` calling `/v2/candles/trade:{tf}:f{currency}:{periodOrAggr}/hist`, parsing the `[mts, open, close, high, low, volume]` array response; populate Symbol="f"+currency, Timeframe, PeriodAgg from request params
- [ ] 2.7 Implement `(c *Client) GetFundingStats(ctx, currency string, start, end int64, limit int) ([]domain.FundingStat, error)` calling `/v2/funding/stats/f{currency}/hist`, parsing the 12-element array (skipping the four null-padding fields at indices 1, 2, 5, 6, 9, 10); populate Symbol="f"+currency
- [ ] 2.8 Add unit test `TestGetFundingCandles_Success` covering canonical response parsing, asserting URL path + limit query param + parsed MTS + parsed OHLCV
- [ ] 2.9 Add unit test `TestGetFundingCandles_EmptyResponse` asserting empty slice (not error) on `[]` body
- [ ] 2.10 Add unit test `TestGetFundingStats_Success` covering null-padding skip and FRR / amount field parsing
- [ ] 2.11 Add unit test `TestDoHTTP_RateLimitBackoff_RetrySucceeds` using `httptest.Server` returning 429 then 200, with `WithRateLimitBackoffBase(50*time.Millisecond)`, asserting elapsed ≥ 50ms and final response is the 200 body
- [ ] 2.12 Add unit test `TestDoHTTP_RateLimitBackoff_AllAttemptsFail` returning 5 consecutive 429, asserting final error contains "rate limit exceeded" and the attempt count
- [ ] 2.13 Add unit test `TestDoHTTP_RateLimitBackoff_ContextCancelled` cancelling ctx mid-backoff, asserting `ctx.Err()` returned immediately
- [ ] 2.14 Add unit test `TestRateLimit_BurstClamping` confirming `WithRateLimit(0.5, 1)` causes 5 sequential calls to take ≥ 8 seconds

## 3. Repository — sqlc Queries

- [ ] 3.1 Create `backend/internal/repository/postgres/query/funding.sql` with named queries: `InsertFundingCandle :exec` (INSERT ... ON CONFLICT (symbol, timeframe, period_agg, mts) DO NOTHING), `InsertFundingStat :exec` (INSERT ... ON CONFLICT (symbol, mts) DO NOTHING), `CountFundingCandles :one`, `CountFundingStats :one`, `GetEarliestCandleMTS :one`, `DetectCandleGaps :many`, `SampleStatCandlePairs :many` (the last for FRR verify; SQL described in design.md D8 — pick 5 funding_stats rows + their nearest funding_candles.close within ±1 hour for the same symbol)
- [ ] 3.2 Create `backend/internal/repository/postgres/funding_market_data.go` thin wrapper exposing `FundingMarketDataRepo` struct with methods matching consumer needs (Insert / Count / GetEarliest / DetectGaps / SampleStatCandlePairs)
- [ ] 3.3 `cd backend && sqlc generate` to regenerate `internal/repository/postgres/sqlc/`; verify new typed functions exist
- [ ] 3.4 (No repository unit test — per project convention; DB interaction covered by smoke run in §5)

## 4. Backfill Command Runner

- [ ] 4.1 Create `backend/cmd/backfill/main.go`: parse flags `-currencies` (CSV, required), `-timeframes` (CSV, default `1h`), `-period-aggr` (default `a30:p2:p30`), `-start` (RFC3339, required), `-end` (RFC3339, default now), `-batch-size` (default 10000), `-also-stats` (default true), `-verify` (default false); validate `start < end`, exit 2 on validation error
- [ ] 4.2 In `cmd/backfill/main.go` build `*pgxpool.Pool` from `os.Getenv("DATABASE_URL")` and bitfinex `Client` with `WithRateLimit(0.5, 1)`; defer pool.Close
- [ ] 4.3 Wire `signal.Notify(SIGINT, SIGTERM)` to a `context.CancelFunc`; in-flight HTTP and DB ops respect ctx
- [ ] 4.4 Create `backend/cmd/backfill/pagination.go` with pure function `paginateBackward(ctx context.Context, fetch func(ctx, start, end int64, limit int) ([]int64, error), startMs, endMs int64, batchSize int) error` — returns when fetch yields empty slice OR earliest mts in batch ≤ startMs
- [ ] 4.5 In `main.go` for each (currency, timeframe) in matrix: call `paginateBackward` wrapping `client.GetFundingCandles` + `repo.InsertFundingCandle` per row; log per-page progress with zap structured fields (currency, timeframe, batch_size, cursor_end_mts)
- [ ] 4.6 If `-also-stats=true`, after candles do same `paginateBackward` per currency for `client.GetFundingStats` + `repo.InsertFundingStat`
- [ ] 4.7 On successful exit print summary: rows inserted per (currency, timeframe), stats inserted per currency, total elapsed wall time
- [ ] 4.8 If `-verify=true`, after backfill: (a) for each (currency, timeframe) call `repo.DetectCandleGaps` and report gaps > expected_interval × 1.5; (b) for each currency call `repo.SampleStatCandlePairs(5)` and print FRR/candle ratio with unit-inference suggestion (per-second / per-day / per-period); handle empty DB → print "nothing to verify, run backfill first" and exit 0
- [ ] 4.9 Add unit test for `paginateBackward` covering: empty first response stops loop (1 fetch call), normal range (cursor sequence correct), earliest mts ≤ startMs stops loop, ctx cancellation surfaces immediately
- [ ] 4.10 Add unit test for FRR unit-inference helper (input ratio 169.2 → "per-second", input ratio 1.0 → "per-day", input ratio ~22 → "per-period"); pure function, no DB

## 5. Verification & Sanity Check (manual smoke)

- [ ] 5.1 Run `cd backend && go build ./...` to confirm whole project still compiles
- [ ] 5.2 Run `cd backend && go test ./internal/bitfinex/... ./cmd/backfill/...` and confirm all new tests pass
- [ ] 5.3 Smoke backfill (small range): `cd backend && go run ./cmd/backfill -currencies UST -timeframes 1h -start 2026-05-01 -end 2026-05-09 -verify` → confirm DB row count > 100 candles + at least 1 stats row + verify output sensible
- [ ] 5.4 Re-run same command → confirm 0 new rows inserted (ON CONFLICT idempotency works)
- [ ] 5.5 Full backfill: `cd backend && go run ./cmd/backfill -currencies UST,USD -timeframes 1h,1D -start 2023-05-09 -end 2026-05-09 -verify` → expect ~7 minute runtime, ~110K candles + ~52K stats inserted
- [ ] 5.6 Update `~/second-brain/wiki/tech/bitfinex-funding-rate-data-sources.md` "FRR 單位待確認" section with confirmed answer from §5.5 verify output

## 6. Documentation & OpenSpec Closeout

- [ ] 6.1 Update `~/second-brain/wiki/projects/bfx-funding-bot.md` Stage 1.2 sub-tasks: tick off completed items, link to this OpenSpec change
- [ ] 6.2 Add entry to `bfx-funding-bot.md` Recent Activity describing the change
- [ ] 6.3 Run `openspec validate "bitfinex-funding-historical-backfill"` and confirm valid
- [ ] 6.4 Run `openspec archive bitfinex-funding-historical-backfill` to move change to `openspec/changes/archive/<date>-bitfinex-funding-historical-backfill/` and sync specs into `openspec/specs/`
