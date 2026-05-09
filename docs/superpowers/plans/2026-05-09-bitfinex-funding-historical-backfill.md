# Bitfinex Funding Historical Backfill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backfill 3 years of Bitfinex `fUST` + `fUSD` funding candles (1h + 1D) and funding stats into Neon PostgreSQL so Stage 1.3-1.5 backtest engine has historical data to score baseline strategies.

**Architecture:** Add 2 public methods to existing `bitfinex.Client` (`GetFundingCandles` / `GetFundingStats`) + rate-limit-aware retry to `doHTTP`. New `cmd/backfill/main.go` runner uses `pgxpool` directly (no fx tree), iterates `(currency × timeframe)` matrix, paginates via pure function `paginateBackward`. Idempotency via `INSERT ON CONFLICT DO NOTHING`. Built-in `--verify` mode runs gap detection + FRR unit ratio analysis after backfill.

**Tech Stack:** Go 1.25 / pgx/v5 / sqlc / golang.org/x/time/rate / sony/gobreaker/v2 / uber/zap. Atlas declarative schema (already migrated).

**Spec source:** `openspec/changes/bitfinex-funding-historical-backfill/{proposal,design,tasks}.md` + `specs/`. **Do not deviate** — if you find a contradiction, stop and surface it.

**Pre-state (already done 2026-05-09):**
- Atlas migration `20260509100512_add_funding_history_tables.sql` applied to Neon. Tables `funding_candles` + `funding_stats` exist, empty.
- OpenSpec change `bitfinex-funding-historical-backfill` validated with 4 artifacts + 3 capability specs.
- `tasks.md` Group 1 (Schema & Migration) all checkboxes ticked.

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `backend/internal/domain/funding_market_data.go` | NEW | Pure domain types `FundingCandle` / `FundingStat` |
| `backend/internal/bitfinex/client.go` | MODIFY | Add `WithRateLimitBackoffBase`, `isRateLimitError`, rate-limit retry in `doHTTP`, `GetFundingCandles`, `GetFundingStats` |
| `backend/internal/bitfinex/client_test.go` | MODIFY | Add 7 unit tests (success/empty/backoff retry × 3/burst clamping/stats null skip) |
| `backend/internal/repository/postgres/query/funding.sql` | NEW | sqlc queries (insert, count, get-earliest, gap-detect, sample-pairs) |
| `backend/internal/repository/postgres/funding_market_data.go` | NEW | Thin repo wrapper exposing `FundingMarketDataRepo` struct |
| `backend/internal/repository/postgres/sqlc/funding.sql.go` | NEW (auto-gen) | sqlc output |
| `backend/cmd/backfill/main.go` | NEW | CLI entry point: flag parsing, pgxpool connect, matrix iteration, summary, verify dispatch |
| `backend/cmd/backfill/pagination.go` | NEW | Pure function `paginateBackward` |
| `backend/cmd/backfill/pagination_test.go` | NEW | Unit tests for `paginateBackward` |
| `backend/cmd/backfill/verify.go` | NEW | `runVerify` orchestrating gap detect + FRR ratio analysis |
| `backend/cmd/backfill/verify_test.go` | NEW | Unit test for FRR unit-inference helper (pure function) |

**No changes to:** existing 5 repos / handler / service / lending engine / migrations / frontend / fx wiring.

---

## Group 2: Bitfinex Client — Public Endpoints + Rate-Limit Backoff

**Estimated time:** 60-75 min
**Checkpoint commit:** at end of group (single commit covers domain types + 2 client methods + backoff + tests). Reason: methods + tests are interdependent and don't compile usefully apart.
**Pitfalls:**
- Existing `doHTTP` is hardcoded to `http.MethodPost` — public GET endpoints need a code path that uses `MethodGet` and constructs query strings, not JSON body. **Refactor `doHTTP` to take an HTTP method + optional body**, OR add a sibling `doPublicGET` helper. **Plan uses sibling helper** to keep `doHTTP` auth-path untouched.
- Existing `parseError` checks `["error", code, msg]`. Public endpoints sometimes return same format on rate limit (HTTP 200 with body `["error",11010,"ratelimit:..."]`). The `isRateLimitError` check must run BEFORE `parseError` swallows it.
- `httptest.Server` returns `ts.URL` like `http://127.0.0.1:54321`. The existing `overrideBaseURL(client, ts.URL)` helper sets `baseURLOverride`. New tests can reuse this pattern.
- Bitfinex candles array fields: `[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]` — note CLOSE comes BEFORE HIGH/LOW (not the standard OHLC order). Verified via 5/9 spot test.
- funding/stats array has 12 elements with nulls at indices 1, 2, 5, 6, 9, 10. Use `[]json.RawMessage` parse and pick indices 0/3/4/7/8/11.

**Subagent / parallel opportunity:** None — single Go file gets several edits; sequential is cleaner.

---

### Task 2.1: Domain types

**Files:**
- Create: `backend/internal/domain/funding_market_data.go`

- [ ] **Step 1: Create the file**

```go
package domain

// FundingCandle is one OHLCV candle from Bitfinex's public funding candles
// endpoint (`/v2/candles/trade:{tf}:f{currency}:{period}/hist`).
//
// Symbol / Timeframe / PeriodAgg are not part of the upstream payload; they
// are populated by the caller from request parameters so persistence layers
// can use them as part of the composite primary key.
type FundingCandle struct {
	Symbol    string  // e.g. "fUST" / "fUSD"
	Timeframe string  // e.g. "1h" / "1D"
	PeriodAgg string  // e.g. "a30:p2:p30" / "p2"
	MTS       int64   // millisecond epoch
	Open      float64 // daily-rate units; multiply by 365 for naive annualisation
	Close     float64
	High      float64
	Low       float64
	Volume    float64
}

// FundingStat is one record from `/v2/funding/stats/f{currency}/hist`.
//
// The Bitfinex array response contains four `null` placeholders at indexes
// 1, 2, 5, 6, 9, 10 which are dropped during parsing.
//
// FRR units (per-second / per-day / per-period) are confirmed by the
// `--verify` step of cmd/backfill on the first full backfill.
type FundingStat struct {
	Symbol                string  // e.g. "fUST"
	MTS                   int64
	FRR                   float64
	AvgPeriod             float64
	FundingAmount         float64
	FundingAmountUsed     float64
	FundingBelowThreshold float64
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd backend && go build ./internal/domain/`
Expected: no output, exit 0.

---

### Task 2.2: Add `WithRateLimitBackoffBase` ClientOption + private field

**Files:**
- Modify: `backend/internal/bitfinex/client.go:21-26` (Client struct), append after `WithRateLimit` (line ~36)

- [ ] **Step 1: Add field to Client struct**

In `client.go`, modify the Client struct (around line 21):

```go
type Client struct {
	httpClient        *http.Client
	cb                *gobreaker.CircuitBreaker[[]byte]
	limiter           *rate.Limiter
	baseURLOverride   string
	rateLimitBackoff  time.Duration // first backoff interval; doubles up to 4×
}
```

- [ ] **Step 2: Add ClientOption after `WithRateLimit`**

After the closing brace of `WithRateLimit` (line ~36):

```go
// WithRateLimitBackoffBase sets the first backoff interval used when the
// upstream returns a rate-limit error. Subsequent attempts double the wait
// up to 4× this value. Default 60s.
func WithRateLimitBackoffBase(d time.Duration) ClientOption {
	return func(c *Client) {
		c.rateLimitBackoff = d
	}
}
```

- [ ] **Step 3: Set default in `newClientInternal`**

In `newClientInternal` (line ~46), where the `Client` struct is constructed (line ~57), add the field:

```go
c := &Client{
	httpClient:       httpClient,
	baseURLOverride:  baseOverride,
	cb:               cb,
	limiter:          rate.NewLimiter(rate.Limit(15), 20),
	rateLimitBackoff: 60 * time.Second,
}
```

- [ ] **Step 4: Verify compile**

Run: `cd backend && go build ./internal/bitfinex/`
Expected: no output.

---

### Task 2.3: Add `isRateLimitError` helper

**Files:**
- Modify: `backend/internal/bitfinex/client.go` (append after `parseError`, ~line 156)

- [ ] **Step 1: Write the test (TDD)**

Add to `backend/internal/bitfinex/client_test.go` (anywhere among existing tests):

```go
func TestIsRateLimitError(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   []byte
		want   bool
	}{
		{"http 429", 429, []byte(`anything`), true},
		{"bitfinex code 11010", 200, []byte(`["error",11010,"ratelimit: request denied"]`), true},
		{"body contains ratelimit:", 200, []byte(`["error",10000,"ratelimit: too fast"]`), true},
		{"normal 200 array", 200, []byte(`[[1778284800000,0.0001,0.0002,0.0003,0.00005,1000.5]]`), false},
		{"non-rate-limit error", 200, []byte(`["error",10001,"internal"]`), false},
		{"empty array", 200, []byte(`[]`), false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isRateLimitError(tt.status, tt.body); got != tt.want {
				t.Errorf("isRateLimitError(%d, %s) = %v, want %v", tt.status, tt.body, got, tt.want)
			}
		})
	}
}
```

- [ ] **Step 2: Run test (expect FAIL — undefined)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestIsRateLimitError -v`
Expected: build error `undefined: isRateLimitError`.

- [ ] **Step 3: Implement `isRateLimitError`**

Add to `client.go` after `parseError` function:

```go
// isRateLimitError detects whether an upstream response is a rate-limit
// signal: HTTP 429, Bitfinex error code 11010, or response body containing
// the literal "ratelimit:".
func isRateLimitError(status int, body []byte) bool {
	if status == 429 {
		return true
	}
	if bytes.Contains(body, []byte(`"ratelimit:`)) {
		return true
	}
	if bytes.Contains(body, []byte("11010")) {
		return true
	}
	return false
}
```

- [ ] **Step 4: Run test (expect PASS)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestIsRateLimitError -v`
Expected: `--- PASS: TestIsRateLimitError`.

---

### Task 2.4: Add `doPublicGET` helper with rate-limit retry

**Rationale:** Existing `doHTTP` is POST-only and adds auth headers. Public endpoints are GET with no auth. Sibling helper keeps blast radius small (auth path untouched), but it shares circuit breaker + rate limiter via the existing wrapper pattern.

**Files:**
- Modify: `backend/internal/bitfinex/client.go` (append after `doHTTP`, ~line 128)

- [ ] **Step 1: Write the test for retry-success path**

Add to `client_test.go`:

```go
func TestDoPublicGET_RateLimitBackoff_RetrySucceeds(t *testing.T) {
	callCount := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount++
		if callCount == 1 {
			w.WriteHeader(429)
			_, _ = w.Write([]byte(`["error",11010,"ratelimit: request denied"]`))
			return
		}
		_, _ = w.Write([]byte(`[]`))
	}))
	defer srv.Close()

	client := NewClient(srv.Client(), WithRateLimitBackoffBase(50*time.Millisecond))
	restore := overrideBaseURL(client, srv.URL)
	defer restore()

	start := time.Now()
	body, err := client.doPublicGET(context.Background(), "v2/candles/trade:1h:fUST:a30:p2:p30/hist", nil)
	elapsed := time.Since(start)

	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if string(body) != `[]` {
		t.Errorf("expected body=[], got %s", body)
	}
	if callCount != 2 {
		t.Errorf("expected 2 calls (1 retry), got %d", callCount)
	}
	if elapsed < 50*time.Millisecond {
		t.Errorf("expected elapsed ≥ 50ms, got %v", elapsed)
	}
}
```

- [ ] **Step 2: Run test (expect FAIL — undefined doPublicGET)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestDoPublicGET_RateLimitBackoff_RetrySucceeds -v`
Expected: build error.

- [ ] **Step 3: Implement `doPublicGET`**

Append to `client.go` after `doHTTP` (line ~128):

```go
// doPublicGET issues a public (no-auth) GET request to a Bitfinex endpoint.
// Path should be like "v2/candles/trade:1h:fUST:a30:p2:p30/hist".
// Query params (if any) are added by the caller via url.Values encoded into the path.
//
// Behaviour:
//   - Respects the Client's rate limiter and circuit breaker
//   - On rate-limit error (HTTP 429 / Bitfinex code 11010 / body contains
//     "ratelimit:"), waits with exponential backoff (base × 2^attempt, cap 4×base)
//     and retries up to 4 times before surfacing the error
//   - Calls parseError for non-rate-limit Bitfinex errors
func (c *Client) doPublicGET(ctx context.Context, apiPath string, query map[string]string) ([]byte, error) {
	if err := c.limiter.Wait(ctx); err != nil {
		return nil, fmt.Errorf("rate limiter: %w", err)
	}

	return c.cb.Execute(func() ([]byte, error) {
		return c.doPublicGETInner(ctx, apiPath, query)
	})
}

func (c *Client) doPublicGETInner(ctx context.Context, apiPath string, query map[string]string) ([]byte, error) {
	const maxRateLimitAttempts = 4

	urlBase := baseURL
	if c.baseURLOverride != "" {
		urlBase = c.baseURLOverride
	}
	fullURL := urlBase + "/" + apiPath
	if len(query) > 0 {
		parts := make([]string, 0, len(query))
		for k, v := range query {
			parts = append(parts, k+"="+v)
		}
		fullURL += "?" + strings.Join(parts, "&")
	}

	for attempt := 0; ; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, fullURL, nil)
		if err != nil {
			return nil, fmt.Errorf("create request: %w", err)
		}
		req.Header.Set("Accept", "application/json")

		resp, err := c.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("http request: %w", err)
		}
		body, readErr := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if readErr != nil {
			return nil, fmt.Errorf("read response: %w", readErr)
		}

		if isRateLimitError(resp.StatusCode, body) {
			if attempt >= maxRateLimitAttempts {
				return nil, fmt.Errorf("rate limit exceeded after %d attempts: %s", attempt+1, body)
			}
			wait := c.rateLimitBackoff << attempt
			if cap := c.rateLimitBackoff * 4; wait > cap {
				wait = cap
			}
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(wait):
			}
			continue
		}

		if err := parseError(body); err != nil {
			return nil, err
		}

		return body, nil
	}
}
```

- [ ] **Step 4: Add `strings` import**

In `client.go` imports (line ~3-15), add `"strings"` if not already present:

```go
import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
	// ...
)
```

- [ ] **Step 5: Run retry-success test (expect PASS)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestDoPublicGET_RateLimitBackoff_RetrySucceeds -v`
Expected: PASS, elapsed ~50ms.

- [ ] **Step 6: Add test for all-attempts-fail**

Append to `client_test.go`:

```go
func TestDoPublicGET_RateLimitBackoff_AllAttemptsFail(t *testing.T) {
	callCount := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount++
		w.WriteHeader(429)
		_, _ = w.Write([]byte(`["error",11010,"ratelimit"]`))
	}))
	defer srv.Close()

	client := NewClient(srv.Client(), WithRateLimitBackoffBase(10*time.Millisecond))
	restore := overrideBaseURL(client, srv.URL)
	defer restore()

	_, err := client.doPublicGET(context.Background(), "v2/candles/foo/hist", nil)
	if err == nil {
		t.Fatal("expected error after all attempts exhausted")
	}
	if !strings.Contains(err.Error(), "rate limit exceeded") {
		t.Errorf("expected error to mention 'rate limit exceeded', got %v", err)
	}
	// 1 initial + 4 retries = 5 calls
	if callCount != 5 {
		t.Errorf("expected 5 calls, got %d", callCount)
	}
}
```

You'll need `"strings"` in test imports.

- [ ] **Step 7: Run test (expect PASS)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestDoPublicGET_RateLimitBackoff_AllAttemptsFail -v`
Expected: PASS, callCount=5.

- [ ] **Step 8: Add test for context cancellation**

```go
func TestDoPublicGET_RateLimitBackoff_ContextCancelled(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(429)
		_, _ = w.Write([]byte(`["error",11010,"ratelimit"]`))
	}))
	defer srv.Close()

	client := NewClient(srv.Client(), WithRateLimitBackoffBase(500*time.Millisecond))
	restore := overrideBaseURL(client, srv.URL)
	defer restore()

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(100 * time.Millisecond)
		cancel()
	}()

	_, err := client.doPublicGET(ctx, "v2/candles/foo/hist", nil)
	if err == nil {
		t.Fatal("expected error from context cancellation")
	}
	if !errors.Is(err, context.Canceled) {
		t.Errorf("expected context.Canceled, got %v", err)
	}
}
```

You'll need `"errors"` in test imports.

- [ ] **Step 9: Run test (expect PASS)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestDoPublicGET_RateLimitBackoff_ContextCancelled -v`
Expected: PASS, completes in <600ms.

---

### Task 2.5: Implement `GetFundingCandles`

**Files:**
- Modify: `backend/internal/bitfinex/client.go` (append at end)

- [ ] **Step 1: Write the test for happy path**

Append to `client_test.go`:

```go
func TestGetFundingCandles_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v2/candles/trade:1h:fUST:a30:p2:p30/hist" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if r.URL.Query().Get("limit") != "10000" {
			t.Errorf("expected limit=10000, got %s", r.URL.Query().Get("limit"))
		}
		_, _ = w.Write([]byte(`[[1778284800000,0.000098,0.000112,0.000175,0.000045,20128759.8],[1778281200000,0.000095,0.000098,0.0001,0.00005,15000.0]]`))
	}))
	defer srv.Close()

	client := NewClient(srv.Client())
	restore := overrideBaseURL(client, srv.URL)
	defer restore()

	candles, err := client.GetFundingCandles(context.Background(), "UST", "1h", "a30:p2:p30", 0, 0, 10000)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(candles) != 2 {
		t.Fatalf("expected 2 candles, got %d", len(candles))
	}
	c := candles[0]
	if c.Symbol != "fUST" || c.Timeframe != "1h" || c.PeriodAgg != "a30:p2:p30" {
		t.Errorf("unexpected key fields: %+v", c)
	}
	if c.MTS != 1778284800000 {
		t.Errorf("MTS mismatch: %d", c.MTS)
	}
	if c.Open != 0.000098 || c.Close != 0.000112 || c.High != 0.000175 || c.Low != 0.000045 {
		t.Errorf("OHLC mismatch: %+v", c)
	}
	if c.Volume != 20128759.8 {
		t.Errorf("Volume mismatch: %v", c.Volume)
	}
}
```

- [ ] **Step 2: Run test (expect FAIL)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestGetFundingCandles_Success -v`
Expected: build error `undefined: GetFundingCandles`.

- [ ] **Step 3: Implement `GetFundingCandles`**

Append to `client.go`:

```go
// GetFundingCandles fetches funding-currency candles from
// `/v2/candles/trade:{timeframe}:f{currency}:{periodOrAggr}/hist`.
//
// currency: "UST" / "USD" / "BTC" / etc. (without the "f" prefix)
// timeframe: "1m" / "5m" / "15m" / "30m" / "1h" / "3h" / "6h" / "12h" / "1D" / "7D" / "14D" / "1M"
// periodOrAggr: "p2" / "p30" / "a30:p2:p30" (aggregation matching Bitfinex UI)
// start / end: ms epoch (0 = omit)
// limit: max 10000
//
// Returns oldest-to-newest? NO — Bitfinex returns newest first. Caller controls.
func (c *Client) GetFundingCandles(
	ctx context.Context,
	currency, timeframe, periodOrAggr string,
	start, end int64,
	limit int,
) ([]domain.FundingCandle, error) {
	apiPath := fmt.Sprintf("v2/candles/trade:%s:f%s:%s/hist", timeframe, currency, periodOrAggr)
	query := map[string]string{}
	if limit > 0 {
		query["limit"] = strconv.Itoa(limit)
	}
	if start > 0 {
		query["start"] = strconv.FormatInt(start, 10)
	}
	if end > 0 {
		query["end"] = strconv.FormatInt(end, 10)
	}

	data, err := c.doPublicGET(ctx, apiPath, query)
	if err != nil {
		return nil, err
	}

	var raw [][]float64
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse candles: %w", err)
	}

	symbol := "f" + currency
	candles := make([]domain.FundingCandle, 0, len(raw))
	for _, row := range raw {
		if len(row) < 6 {
			continue
		}
		candles = append(candles, domain.FundingCandle{
			Symbol:    symbol,
			Timeframe: timeframe,
			PeriodAgg: periodOrAggr,
			MTS:       int64(row[0]),
			Open:      row[1],
			Close:     row[2],
			High:      row[3],
			Low:       row[4],
			Volume:    row[5],
		})
	}
	return candles, nil
}
```

- [ ] **Step 4: Run test (expect PASS)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestGetFundingCandles_Success -v`
Expected: PASS.

- [ ] **Step 5: Add empty-response test**

Append to `client_test.go`:

```go
func TestGetFundingCandles_EmptyResponse(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[]`))
	}))
	defer srv.Close()

	client := NewClient(srv.Client())
	restore := overrideBaseURL(client, srv.URL)
	defer restore()

	candles, err := client.GetFundingCandles(context.Background(), "USDT", "1h", "a30:p2:p30", 0, 0, 100)
	if err != nil {
		t.Fatalf("expected nil error for empty response, got %v", err)
	}
	if len(candles) != 0 {
		t.Errorf("expected empty slice, got %d candles", len(candles))
	}
}
```

- [ ] **Step 6: Run test (expect PASS)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestGetFundingCandles_EmptyResponse -v`
Expected: PASS.

---

### Task 2.6: Implement `GetFundingStats`

**Files:**
- Modify: `backend/internal/bitfinex/client.go` (append after GetFundingCandles)

- [ ] **Step 1: Write the test**

Append to `client_test.go`:

```go
func TestGetFundingStats_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v2/funding/stats/fUST/hist" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		// 12-element array with nulls at 1, 2, 5, 6, 9, 10
		_, _ = w.Write([]byte(`[[1778313900000,null,null,5.8e-7,22.54,null,null,230400898.71,127770632.21,null,null,219235112.41]]`))
	}))
	defer srv.Close()

	client := NewClient(srv.Client())
	restore := overrideBaseURL(client, srv.URL)
	defer restore()

	stats, err := client.GetFundingStats(context.Background(), "UST", 0, 0, 250)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(stats) != 1 {
		t.Fatalf("expected 1 stat, got %d", len(stats))
	}
	s := stats[0]
	if s.Symbol != "fUST" {
		t.Errorf("expected Symbol=fUST, got %s", s.Symbol)
	}
	if s.MTS != 1778313900000 {
		t.Errorf("MTS mismatch: %d", s.MTS)
	}
	if s.FRR != 5.8e-7 {
		t.Errorf("FRR mismatch: %v", s.FRR)
	}
	if s.AvgPeriod != 22.54 {
		t.Errorf("AvgPeriod mismatch: %v", s.AvgPeriod)
	}
	if s.FundingAmount != 230400898.71 {
		t.Errorf("FundingAmount mismatch: %v", s.FundingAmount)
	}
	if s.FundingAmountUsed != 127770632.21 {
		t.Errorf("FundingAmountUsed mismatch: %v", s.FundingAmountUsed)
	}
	if s.FundingBelowThreshold != 219235112.41 {
		t.Errorf("FundingBelowThreshold mismatch: %v", s.FundingBelowThreshold)
	}
}
```

- [ ] **Step 2: Run test (expect FAIL)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestGetFundingStats_Success -v`
Expected: build error.

- [ ] **Step 3: Implement `GetFundingStats`**

Append to `client.go`:

```go
// GetFundingStats fetches funding statistics from `/v2/funding/stats/f{currency}/hist`.
// Each response row is a 12-element array; indexes 1, 2, 5, 6, 9, 10 are null
// placeholders and are dropped.
func (c *Client) GetFundingStats(
	ctx context.Context,
	currency string,
	start, end int64,
	limit int,
) ([]domain.FundingStat, error) {
	apiPath := fmt.Sprintf("v2/funding/stats/f%s/hist", currency)
	query := map[string]string{}
	if limit > 0 {
		query["limit"] = strconv.Itoa(limit)
	}
	if start > 0 {
		query["start"] = strconv.FormatInt(start, 10)
	}
	if end > 0 {
		query["end"] = strconv.FormatInt(end, 10)
	}

	data, err := c.doPublicGET(ctx, apiPath, query)
	if err != nil {
		return nil, err
	}

	var raw [][]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse stats: %w", err)
	}

	symbol := "f" + currency
	stats := make([]domain.FundingStat, 0, len(raw))
	for _, row := range raw {
		if len(row) < 12 {
			continue
		}
		var (
			mts                                                    int64
			frr, avgPeriod, fundingAmount, used, belowThreshold    float64
		)
		_ = json.Unmarshal(row[0], &mts)
		_ = json.Unmarshal(row[3], &frr)
		_ = json.Unmarshal(row[4], &avgPeriod)
		_ = json.Unmarshal(row[7], &fundingAmount)
		_ = json.Unmarshal(row[8], &used)
		_ = json.Unmarshal(row[11], &belowThreshold)

		stats = append(stats, domain.FundingStat{
			Symbol:                symbol,
			MTS:                   mts,
			FRR:                   frr,
			AvgPeriod:             avgPeriod,
			FundingAmount:         fundingAmount,
			FundingAmountUsed:     used,
			FundingBelowThreshold: belowThreshold,
		})
	}
	return stats, nil
}
```

- [ ] **Step 4: Run test (expect PASS)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestGetFundingStats_Success -v`
Expected: PASS.

---

### Task 2.7: Add burst rate-limit clamping test (covers Spec scenario)

**Files:**
- Modify: `backend/internal/bitfinex/client_test.go` (append)

- [ ] **Step 1: Write test**

```go
func TestGetFundingCandles_BurstRateLimitClamping(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[]`))
	}))
	defer srv.Close()

	// 0.5 req/sec, burst 1 → 5 sequential calls take ≥ 8 seconds (4 inter-call waits × 2s)
	client := NewClient(srv.Client(), WithRateLimit(0.5, 1))
	restore := overrideBaseURL(client, srv.URL)
	defer restore()

	start := time.Now()
	for i := 0; i < 5; i++ {
		if _, err := client.GetFundingCandles(context.Background(), "UST", "1h", "a30:p2:p30", 0, 0, 10); err != nil {
			t.Fatalf("call %d failed: %v", i, err)
		}
	}
	elapsed := time.Since(start)
	if elapsed < 8*time.Second {
		t.Errorf("expected elapsed ≥ 8s, got %v", elapsed)
	}
}
```

> ⚠️ This test takes 8+ seconds. Consider running with `-short` flag protection. For now: leave as-is; CI tolerates short integration-flavoured tests.

- [ ] **Step 2: Run test (expect PASS, ~8s)**

Run: `cd backend && go test ./internal/bitfinex/ -run TestGetFundingCandles_BurstRateLimitClamping -v -timeout 30s`
Expected: PASS, elapsed ≥ 8s.

---

### Task 2.8: Run full bitfinex test suite + commit Group 2

- [ ] **Step 1: Run all bitfinex tests**

Run: `cd backend && go test ./internal/bitfinex/... -v -timeout 60s`
Expected: all PASS (existing + 7 new tests). Watch for any regression in existing tests.

- [ ] **Step 2: Run lint**

Run: `cd backend && go vet ./internal/bitfinex/... ./internal/domain/...`
Expected: no output.

- [ ] **Step 3: Commit Group 2**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend/internal/domain/funding_market_data.go \
        backend/internal/bitfinex/client.go \
        backend/internal/bitfinex/client_test.go
git status   # confirm only those 3 files staged
git commit -m "✨ Feat: add Bitfinex public funding endpoints + rate-limit backoff retry

OpenSpec: bitfinex-funding-historical-backfill (group 2/6)

- domain.FundingCandle / FundingStat types
- Client.GetFundingCandles / GetFundingStats public REST methods
- Client.doPublicGET sibling helper (shares limiter + circuit breaker)
- isRateLimitError detector + exponential backoff (60→120→240→240, cap 4)
- WithRateLimitBackoffBase ClientOption for test injection
- 7 new unit tests covering success / empty / null-skip / backoff x3 / burst clamping"
```

**Checkpoint:** Group 2 committed. `bitfinex.Client` now has public funding endpoints with bullet-proof rate-limit handling. Repo + cmd come next.

---

## Group 3: Repository — sqlc Queries

**Estimated time:** 20-30 min
**Checkpoint commit:** at end of group (single commit covers SQL + generated code + repo wrapper).
**Pitfalls:**
- `sqlc generate` runs from `backend/` (per `backend/CLAUDE.md` quick-commands table). Don't accidentally `cd backend/internal/...` first.
- sqlc generates Go code from SQL types. `bigint` → `int64`, `double precision NULL` → `pgtype.Float8` (not float64). The repo wrapper must convert.
- `MIN(mts)` over an empty table returns NULL → sqlc generates `pgtype.Int8` (nullable). Wrapper checks `.Valid`.
- `lead()` window function in `DetectCandleGaps` is supported by Postgres 14+; Neon defaults to recent versions, no concern.

**Subagent / parallel opportunity:** None — generation step is sequential; sqlc.yaml drives output.

---

### Task 3.1: Write SQL queries

**Files:**
- Create: `backend/internal/repository/postgres/query/funding.sql`

- [ ] **Step 1: Create file**

```sql
-- name: InsertFundingCandle :exec
INSERT INTO funding_candles (symbol, timeframe, period_agg, mts, open, close, high, low, volume)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (symbol, timeframe, period_agg, mts) DO NOTHING;

-- name: InsertFundingStat :exec
INSERT INTO funding_stats (symbol, mts, frr, avg_period, funding_amount, funding_amount_used, funding_below_threshold)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (symbol, mts) DO NOTHING;

-- name: CountFundingCandles :one
SELECT count(*)::bigint AS n
FROM funding_candles
WHERE symbol = $1 AND timeframe = $2 AND period_agg = $3;

-- name: CountFundingStats :one
SELECT count(*)::bigint AS n
FROM funding_stats
WHERE symbol = $1;

-- name: GetEarliestCandleMTS :one
SELECT MIN(mts)::bigint AS earliest
FROM funding_candles
WHERE symbol = $1 AND timeframe = $2 AND period_agg = $3;

-- name: DetectCandleGaps :many
SELECT
    mts,
    (lead(mts) OVER (ORDER BY mts) - mts)::bigint AS gap_ms
FROM funding_candles
WHERE symbol = $1 AND timeframe = $2 AND period_agg = $3
ORDER BY mts;

-- name: SampleStatCandlePairs :many
-- Pairs each of the most recent N funding_stats rows with the funding_candles
-- close at the same MTS (timeframe='1h', period_agg='a30:p2:p30') within ±1 hour.
-- Used by --verify to compute FRR/candle ratio for unit inference.
SELECT
    s.mts            AS stat_mts,
    s.frr            AS frr,
    c.close          AS candle_close,
    (s.mts - c.mts)  AS offset_ms
FROM funding_stats s
JOIN LATERAL (
    SELECT mts, close
    FROM funding_candles
    WHERE symbol = s.symbol
      AND timeframe = '1h'
      AND period_agg = 'a30:p2:p30'
      AND mts BETWEEN s.mts - 3600000 AND s.mts + 3600000
    ORDER BY abs(c.mts - s.mts)
    LIMIT 1
) c ON true
WHERE s.symbol = $1
  AND s.frr IS NOT NULL
  AND c.close IS NOT NULL
ORDER BY s.mts DESC
LIMIT $2;
```

> ⚠️ Note: `abs(c.mts - s.mts)` in the LATERAL subquery references `c.mts` from inside the subquery. If the SQL doesn't compile via sqlc, change `ORDER BY abs(c.mts - s.mts)` → `ORDER BY abs(mts - s.mts)`. Verify in Step 2.

- [ ] **Step 2: Generate sqlc code**

Run: `cd backend && sqlc generate`
Expected: no errors. Check `internal/repository/postgres/sqlc/funding.sql.go` exists.

If error mentions LATERAL or window function: simplify `SampleStatCandlePairs` to a simpler approximation:

```sql
-- name: SampleStatCandlePairs :many
SELECT s.mts AS stat_mts, s.frr, c.close AS candle_close, 0::bigint AS offset_ms
FROM funding_stats s
JOIN funding_candles c
  ON c.symbol = s.symbol
 AND c.timeframe = '1h'
 AND c.period_agg = 'a30:p2:p30'
 AND c.mts BETWEEN s.mts - 3600000 AND s.mts + 3600000
WHERE s.symbol = $1
  AND s.frr IS NOT NULL
  AND c.close IS NOT NULL
ORDER BY s.mts DESC
LIMIT $2;
```

This may return multiple rows per stat (one per matching candle in window). The wrapper layer dedupes by stat_mts.

- [ ] **Step 3: Verify build**

Run: `cd backend && go build ./internal/repository/...`
Expected: no output.

---

### Task 3.2: Write repo wrapper

**Files:**
- Create: `backend/internal/repository/postgres/funding_market_data.go`

- [ ] **Step 1: Write the wrapper**

```go
package postgres

import (
	"context"
	"database/sql"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres/sqlc"
)

type FundingMarketDataRepo struct {
	q *sqlc.Queries
}

func NewFundingMarketDataRepo(q *sqlc.Queries) *FundingMarketDataRepo {
	return &FundingMarketDataRepo{q: q}
}

func (r *FundingMarketDataRepo) InsertCandle(ctx context.Context, c domain.FundingCandle) error {
	return r.q.InsertFundingCandle(ctx, sqlc.InsertFundingCandleParams{
		Symbol:    c.Symbol,
		Timeframe: c.Timeframe,
		PeriodAgg: c.PeriodAgg,
		Mts:       c.MTS,
		Open:      pgtype.Float8{Float64: c.Open, Valid: true},
		Close:     pgtype.Float8{Float64: c.Close, Valid: true},
		High:      pgtype.Float8{Float64: c.High, Valid: true},
		Low:       pgtype.Float8{Float64: c.Low, Valid: true},
		Volume:    pgtype.Float8{Float64: c.Volume, Valid: true},
	})
}

func (r *FundingMarketDataRepo) InsertStat(ctx context.Context, s domain.FundingStat) error {
	return r.q.InsertFundingStat(ctx, sqlc.InsertFundingStatParams{
		Symbol:                s.Symbol,
		Mts:                   s.MTS,
		Frr:                   pgtype.Float8{Float64: s.FRR, Valid: true},
		AvgPeriod:             pgtype.Float8{Float64: s.AvgPeriod, Valid: true},
		FundingAmount:         pgtype.Float8{Float64: s.FundingAmount, Valid: true},
		FundingAmountUsed:     pgtype.Float8{Float64: s.FundingAmountUsed, Valid: true},
		FundingBelowThreshold: pgtype.Float8{Float64: s.FundingBelowThreshold, Valid: true},
	})
}

func (r *FundingMarketDataRepo) CountCandles(ctx context.Context, symbol, timeframe, periodAgg string) (int64, error) {
	return r.q.CountFundingCandles(ctx, sqlc.CountFundingCandlesParams{
		Symbol:    symbol,
		Timeframe: timeframe,
		PeriodAgg: periodAgg,
	})
}

func (r *FundingMarketDataRepo) CountStats(ctx context.Context, symbol string) (int64, error) {
	return r.q.CountFundingStats(ctx, symbol)
}

// GetEarliestCandleMTS returns the smallest mts persisted for the given key
// combination, or sql.ErrNoRows if the table is empty for that key.
func (r *FundingMarketDataRepo) GetEarliestCandleMTS(ctx context.Context, symbol, timeframe, periodAgg string) (int64, error) {
	v, err := r.q.GetEarliestCandleMTS(ctx, sqlc.GetEarliestCandleMTSParams{
		Symbol:    symbol,
		Timeframe: timeframe,
		PeriodAgg: periodAgg,
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, sql.ErrNoRows
		}
		return 0, err
	}
	return v, nil
}

type CandleGap struct {
	MTS    int64
	GapMS  int64
}

func (r *FundingMarketDataRepo) DetectCandleGaps(ctx context.Context, symbol, timeframe, periodAgg string, minGapMS int64) ([]CandleGap, error) {
	rows, err := r.q.DetectCandleGaps(ctx, sqlc.DetectCandleGapsParams{
		Symbol:    symbol,
		Timeframe: timeframe,
		PeriodAgg: periodAgg,
	})
	if err != nil {
		return nil, err
	}
	out := make([]CandleGap, 0)
	for _, row := range rows {
		if row.GapMs > minGapMS {
			out = append(out, CandleGap{MTS: row.Mts, GapMS: row.GapMs})
		}
	}
	return out, nil
}

type StatCandlePair struct {
	StatMTS     int64
	FRR         float64
	CandleClose float64
}

func (r *FundingMarketDataRepo) SampleStatCandlePairs(ctx context.Context, symbol string, limit int) ([]StatCandlePair, error) {
	rows, err := r.q.SampleStatCandlePairs(ctx, sqlc.SampleStatCandlePairsParams{
		Symbol: symbol,
		Limit:  int32(limit),
	})
	if err != nil {
		return nil, err
	}
	seen := make(map[int64]bool)
	out := make([]StatCandlePair, 0, limit)
	for _, row := range rows {
		if seen[row.StatMts] {
			continue
		}
		seen[row.StatMts] = true
		out = append(out, StatCandlePair{
			StatMTS:     row.StatMts,
			FRR:         row.Frr.Float64,
			CandleClose: row.CandleClose.Float64,
		})
		if len(out) >= limit {
			break
		}
	}
	return out, nil
}
```

> ⚠️ Field names like `Mts` (not `MTS`) match sqlc's lowercased-then-Go-cased convention. After Step 2 below, if compile fails, run `grep -i "mts" internal/repository/postgres/sqlc/funding.sql.go` and align field names exactly.

- [ ] **Step 2: Verify build**

Run: `cd backend && go build ./internal/repository/postgres/...`
Expected: no output. Fix field-name capitalization mismatches if any.

---

### Task 3.3: Commit Group 3

- [ ] **Step 1: Stage and commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend/internal/repository/postgres/query/funding.sql \
        backend/internal/repository/postgres/funding_market_data.go \
        backend/internal/repository/postgres/sqlc/
git status  # confirm sqlc.yaml unchanged + new files staged
git commit -m "✨ Feat: add funding market data repository (candles + stats)

OpenSpec: bitfinex-funding-historical-backfill (group 3/6)

- query/funding.sql: 7 named queries (insert / count / earliest / gap / sample-pairs)
- funding_market_data.go: thin repo wrapper exposing FundingMarketDataRepo
- sqlc-generated funding.sql.go (auto)
- ON CONFLICT DO NOTHING for both insert paths (idempotent backfill)
- No _test.go (per project convention; covered by group 5 smoke run)"
```

**Checkpoint:** Group 3 committed. Repo layer ready to consume from cmd.

---

## Group 4: Backfill Command Runner

**Estimated time:** 60-75 min
**Checkpoint commits:** Two — one after `paginateBackward` + tests (clean small commit), one after full main.go + verify.go.
**Pitfalls:**
- `pgxpool.Connect` is deprecated; use `pgxpool.New(ctx, dsn)`. Confirm via `grep -r pgxpool internal/infra/`.
- `flag` package's CSV parsing requires manual `strings.Split`. Validate non-empty after split.
- `signal.Notify` MUST run before any long blocking call. Defer `signal.Stop`.
- Bitfinex returns candles **newest-first**; the cursor logic in `paginateBackward` assumes this. Document explicitly.
- `--start` / `--end` flag values: accept BOTH RFC3339 (`2023-05-09`) and millisecond integer for ergonomics.
- For `funding/stats`, max limit is 250 (not 10000). The runner needs to use the right limit per endpoint type. Plan: pass batch size as parameter to each call site.
- zap default constructor `zap.NewProduction()` writes JSON to stderr. For cmd output, prefer `zap.NewDevelopment()` for human readability.

**Subagent / parallel opportunity:** `paginateBackward` test file can be written by a subagent in parallel with main.go if needed, but the saving is small; sequential is fine.

---

### Task 4.1: Pagination cursor — pure function + tests

**Files:**
- Create: `backend/cmd/backfill/pagination.go`
- Create: `backend/cmd/backfill/pagination_test.go`

- [ ] **Step 1: Write the test FIRST**

`backend/cmd/backfill/pagination_test.go`:

```go
package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

// mockFetch returns batches in sequence. Each batch is a slice of MTS values
// that the fetcher would return (newest-first). After exhausting `batches`,
// returns empty slice indefinitely.
func mockFetch(batches [][]int64) func(ctx context.Context, start, end int64, limit int) ([]int64, error) {
	idx := 0
	return func(_ context.Context, _, _ int64, _ int) ([]int64, error) {
		if idx >= len(batches) {
			return []int64{}, nil
		}
		b := batches[idx]
		idx++
		return b, nil
	}
}

func TestPaginateBackward_EmptyFirstResponse(t *testing.T) {
	calls := 0
	fetch := func(_ context.Context, _, _ int64, _ int) ([]int64, error) {
		calls++
		return []int64{}, nil
	}
	if err := paginateBackward(context.Background(), fetch, 1000, 10000, 100); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if calls != 1 {
		t.Errorf("expected 1 fetch call, got %d", calls)
	}
}

func TestPaginateBackward_StopsWhenEarliestReachesStart(t *testing.T) {
	calls := 0
	batches := [][]int64{
		{5000, 4000, 3000},
		{2500, 1500, 999}, // 999 ≤ startMs=1000 → stop
		{500, 200},        // should NOT be requested
	}
	fetch := func(_ context.Context, _, _ int64, _ int) ([]int64, error) {
		calls++
		return mockFetch(batches)(nil, 0, 0, 0), nil
	}
	// re-create stateful mock:
	mock := mockFetch(batches)
	wrappedFetch := func(ctx context.Context, start, end int64, limit int) ([]int64, error) {
		calls++
		return mock(ctx, start, end, limit)
	}

	if err := paginateBackward(context.Background(), wrappedFetch, 1000, 10000, 100); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if calls != 2 {
		t.Errorf("expected 2 fetch calls, got %d", calls)
	}
}

func TestPaginateBackward_FetchError(t *testing.T) {
	wantErr := errors.New("upstream broken")
	fetch := func(_ context.Context, _, _ int64, _ int) ([]int64, error) {
		return nil, wantErr
	}
	err := paginateBackward(context.Background(), fetch, 1000, 10000, 100)
	if !errors.Is(err, wantErr) {
		t.Errorf("expected error %v, got %v", wantErr, err)
	}
}

func TestPaginateBackward_ContextCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	calls := 0
	fetch := func(c context.Context, _, _ int64, _ int) ([]int64, error) {
		calls++
		if calls == 1 {
			cancel()
		}
		// Mimic a real fetch checking ctx
		select {
		case <-c.Done():
			return nil, c.Err()
		default:
			return []int64{5000, 4000}, nil
		}
	}
	err := paginateBackward(ctx, fetch, 1000, 10000, 100)
	// Either return ctx.Err on next iteration, or fetch returns it
	if err == nil {
		t.Fatal("expected error from context cancellation")
	}
	_ = time.Now() // prevent unused import if removed later
}
```

- [ ] **Step 2: Run tests (expect FAIL — undefined paginateBackward)**

Run: `cd backend && go test ./cmd/backfill/ -v`
Expected: build error.

- [ ] **Step 3: Implement `paginateBackward`**

`backend/cmd/backfill/pagination.go`:

```go
package main

import (
	"context"
	"fmt"
)

// FetchFunc is the contract callers provide to paginateBackward. It fetches
// one batch of records (whose only relevant field for pagination is MTS) for
// the given start/end window and limit, returning newest-first.
type FetchFunc func(ctx context.Context, start, end int64, limit int) ([]int64, error)

// paginateBackward iterates from endMs backwards toward startMs, calling
// `fetch` with progressively smaller `end` until either:
//   - `fetch` returns an empty slice (no more data)
//   - the earliest MTS in a batch is ≤ startMs (we've covered the requested range)
//   - `ctx` is cancelled
//   - `fetch` returns an error
func paginateBackward(ctx context.Context, fetch FetchFunc, startMs, endMs int64, batchSize int) error {
	cursor := endMs
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		batch, err := fetch(ctx, startMs, cursor, batchSize)
		if err != nil {
			return fmt.Errorf("fetch (cursor=%d): %w", cursor, err)
		}
		if len(batch) == 0 {
			return nil
		}

		earliest := batch[0]
		for _, mts := range batch[1:] {
			if mts < earliest {
				earliest = mts
			}
		}

		if earliest <= startMs {
			return nil
		}

		cursor = earliest - 1
	}
}
```

- [ ] **Step 4: Run tests (expect PASS)**

Run: `cd backend && go test ./cmd/backfill/ -v`
Expected: all pagination tests PASS.

- [ ] **Step 5: Commit pagination unit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend/cmd/backfill/pagination.go backend/cmd/backfill/pagination_test.go
git commit -m "✨ Feat: add paginateBackward pure function for backfill cursor logic

OpenSpec: bitfinex-funding-historical-backfill (group 4 / partial)

- FetchFunc contract for testable backfill pagination
- 4 unit tests: empty / earliest-reaches-start / fetch-error / ctx-cancelled
- Pure function, no HTTP/DB deps"
```

---

### Task 4.2: Verify helpers — FRR unit inference

**Files:**
- Create: `backend/cmd/backfill/verify.go`
- Create: `backend/cmd/backfill/verify_test.go`

- [ ] **Step 1: Write test for FRR unit inference**

`backend/cmd/backfill/verify_test.go`:

```go
package main

import "testing"

func TestInferFRRUnit(t *testing.T) {
	tests := []struct {
		name      string
		ratio     float64
		wantUnit  string
	}{
		{"ratio ~169 → per-second", 169.2, "per-second"},
		{"ratio ~86400 / 512 boundary", 168.75, "per-second"},
		{"ratio ~1 → per-day", 1.0, "per-day"},
		{"ratio ~22 → per-period(22d)", 22.0, "per-period"},
		{"ratio 0.001 → unknown low", 0.001, "unknown"},
		{"ratio 1e6 → unknown high", 1e6, "unknown"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := inferFRRUnit(tt.ratio); got != tt.wantUnit {
				t.Errorf("inferFRRUnit(%v) = %q, want %q", tt.ratio, got, tt.wantUnit)
			}
		})
	}
}
```

- [ ] **Step 2: Run test (expect FAIL)**

Run: `cd backend && go test ./cmd/backfill/ -run TestInferFRRUnit -v`
Expected: build error.

- [ ] **Step 3: Implement `inferFRRUnit` (skeleton verify.go)**

`backend/cmd/backfill/verify.go`:

```go
package main

import (
	"context"
	"fmt"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres"
)

// inferFRRUnit takes the ratio (candle_close / FRR) sampled at the same time
// and returns a human-readable unit hypothesis.
//
// Mathematical bases:
//   - If FRR is per-second: candle (per-day) / FRR (per-second) ≈ 86400, but
//     market dispersion and FRR != close means the observed ratio for current
//     market clusters around 100-300 (5/9 spot test: 169.2)
//   - If FRR is per-day: ratio ≈ 1 (within market dispersion 0.5-2)
//   - If FRR is per-period (~22d): ratio ≈ 22 (within 10-50)
func inferFRRUnit(ratio float64) string {
	switch {
	case ratio >= 50 && ratio <= 500:
		return "per-second"
	case ratio >= 0.5 && ratio <= 2:
		return "per-day"
	case ratio >= 10 && ratio <= 50:
		return "per-period"
	default:
		return "unknown"
	}
}

// runVerify executes gap detection + FRR ratio analysis after backfill.
// Returns nil if both checks ran (regardless of findings); error only if a
// query itself fails. Findings are logged via the supplied logger.
func runVerify(
	ctx context.Context,
	log *zap.Logger,
	repo *postgres.FundingMarketDataRepo,
	currencies []string,
	timeframes []string,
	periodAgg string,
) error {
	// Empty-DB short circuit
	totalCandles := int64(0)
	for _, cur := range currencies {
		for _, tf := range timeframes {
			n, err := repo.CountCandles(ctx, "f"+cur, tf, periodAgg)
			if err != nil {
				return fmt.Errorf("count candles (%s, %s): %w", cur, tf, err)
			}
			totalCandles += n
		}
	}
	if totalCandles == 0 {
		log.Info("verify: nothing to verify, run backfill first")
		return nil
	}

	// Gap detection
	for _, cur := range currencies {
		symbol := "f" + cur
		for _, tf := range timeframes {
			expected := expectedIntervalMs(tf)
			minGap := int64(float64(expected) * 1.5)
			gaps, err := repo.DetectCandleGaps(ctx, symbol, tf, periodAgg, minGap)
			if err != nil {
				return fmt.Errorf("gap detect (%s, %s): %w", cur, tf, err)
			}
			if len(gaps) == 0 {
				log.Info("verify: gaps", zap.String("symbol", symbol), zap.String("timeframe", tf), zap.String("result", "no large gaps"))
				continue
			}
			biggestMs := gaps[0].GapMS
			for _, g := range gaps[1:] {
				if g.GapMS > biggestMs {
					biggestMs = g.GapMS
				}
			}
			log.Warn("verify: gaps detected",
				zap.String("symbol", symbol),
				zap.String("timeframe", tf),
				zap.Int("count", len(gaps)),
				zap.Int64("biggest_ms", biggestMs))
		}
	}

	// FRR unit inference per currency
	for _, cur := range currencies {
		symbol := "f" + cur
		pairs, err := repo.SampleStatCandlePairs(ctx, symbol, 5)
		if err != nil {
			return fmt.Errorf("sample pairs (%s): %w", cur, err)
		}
		if len(pairs) == 0 {
			log.Info("verify: FRR ratio inconclusive", zap.String("symbol", symbol), zap.String("reason", "no overlapping samples in ±1h window"))
			continue
		}
		ratioSum := 0.0
		for _, p := range pairs {
			if p.FRR == 0 {
				continue
			}
			ratioSum += p.CandleClose / p.FRR
		}
		avgRatio := ratioSum / float64(len(pairs))
		unit := inferFRRUnit(avgRatio)
		log.Info("verify: FRR unit inference",
			zap.String("symbol", symbol),
			zap.Int("samples", len(pairs)),
			zap.Float64("avg_ratio", avgRatio),
			zap.String("inferred_unit", unit))
	}

	return nil
}

func expectedIntervalMs(timeframe string) int64 {
	switch timeframe {
	case "1m":
		return 60_000
	case "5m":
		return 5 * 60_000
	case "15m":
		return 15 * 60_000
	case "30m":
		return 30 * 60_000
	case "1h":
		return 60 * 60_000
	case "3h":
		return 3 * 60 * 60_000
	case "6h":
		return 6 * 60 * 60_000
	case "12h":
		return 12 * 60 * 60_000
	case "1D":
		return 24 * 60 * 60_000
	case "7D":
		return 7 * 24 * 60 * 60_000
	case "14D":
		return 14 * 24 * 60 * 60_000
	default:
		return 60 * 60_000 // safe fallback for unknown
	}
}
```

- [ ] **Step 4: Run test (expect PASS)**

Run: `cd backend && go test ./cmd/backfill/ -run TestInferFRRUnit -v`
Expected: PASS.

---

### Task 4.3: Main entry point

**Files:**
- Create: `backend/cmd/backfill/main.go`

- [ ] **Step 1: Write main.go**

```go
// Package main is the bitfinex funding historical backfill runner.
//
// Usage:
//   go run ./cmd/backfill -currencies UST,USD -timeframes 1h,1D \
//                         -start 2023-05-09 -end 2026-05-09 -verify
//
// See OpenSpec change `bitfinex-funding-historical-backfill` for design.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres"
	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres/sqlc"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "backfill failed: %v\n", err)
		os.Exit(1)
	}
}

type config struct {
	Currencies []string
	Timeframes []string
	PeriodAgg  string
	Start      time.Time
	End        time.Time
	BatchSize  int
	AlsoStats  bool
	Verify     bool
}

func parseFlags() (config, error) {
	var (
		currencies = flag.String("currencies", "", "CSV of currencies, e.g. UST,USD (required)")
		timeframes = flag.String("timeframes", "1h", "CSV of timeframes, e.g. 1h,1D")
		periodAgg  = flag.String("period-aggr", "a30:p2:p30", "Bitfinex funding period or aggregation key")
		start      = flag.String("start", "", "Start (RFC3339 date or millis epoch, required)")
		end        = flag.String("end", "", "End (RFC3339 date or millis epoch, default now)")
		batchSize  = flag.Int("batch-size", 10000, "Per-page candle limit (max 10000; stats use 250)")
		alsoStats  = flag.Bool("also-stats", true, "Also backfill funding/stats for each currency")
		verify     = flag.Bool("verify", false, "Run gap-detection + FRR unit inference after backfill")
	)
	flag.Parse()

	if *currencies == "" {
		return config{}, fmt.Errorf("missing required flag -currencies")
	}
	if *start == "" {
		return config{}, fmt.Errorf("missing required flag -start")
	}

	cfg := config{
		Currencies: splitCSV(*currencies),
		Timeframes: splitCSV(*timeframes),
		PeriodAgg:  *periodAgg,
		BatchSize:  *batchSize,
		AlsoStats:  *alsoStats,
		Verify:     *verify,
	}
	var err error
	cfg.Start, err = parseTimestamp(*start)
	if err != nil {
		return cfg, fmt.Errorf("parse -start: %w", err)
	}
	if *end == "" {
		cfg.End = time.Now()
	} else {
		cfg.End, err = parseTimestamp(*end)
		if err != nil {
			return cfg, fmt.Errorf("parse -end: %w", err)
		}
	}
	if !cfg.Start.Before(cfg.End) {
		return cfg, fmt.Errorf("-start must be before -end")
	}
	return cfg, nil
}

func splitCSV(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func parseTimestamp(s string) (time.Time, error) {
	if ms, err := strconv.ParseInt(s, 10, 64); err == nil {
		return time.UnixMilli(ms), nil
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t, nil
	}
	if t, err := time.Parse("2006-01-02", s); err == nil {
		return t, nil
	}
	return time.Time{}, fmt.Errorf("unrecognised timestamp %q", s)
}

func run() error {
	cfg, err := parseFlags()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2) // flag validation error
	}

	logger, _ := zap.NewDevelopment()
	defer func() { _ = logger.Sync() }()

	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		return fmt.Errorf("DATABASE_URL env var is empty")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(sigCh)
	go func() {
		<-sigCh
		logger.Warn("shutdown signal received, cancelling context")
		cancel()
	}()

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return fmt.Errorf("connect db: %w", err)
	}
	defer pool.Close()

	queries := sqlc.New(pool)
	repo := postgres.NewFundingMarketDataRepo(queries)

	client := bitfinex.NewClient(nil, bitfinex.WithRateLimit(0.5, 1))

	startMs := cfg.Start.UnixMilli()
	endMs := cfg.End.UnixMilli()

	totalStart := time.Now()
	type candleStat struct {
		currency, timeframe string
		fetched, inserted   int
	}
	var candleStats []candleStat
	type statStat struct {
		currency          string
		fetched, inserted int
	}
	var statStats []statStat

	// Candles matrix
	for _, cur := range cfg.Currencies {
		for _, tf := range cfg.Timeframes {
			cs := candleStat{currency: cur, timeframe: tf}
			fetch := func(ctx context.Context, start, end int64, limit int) ([]int64, error) {
				batch, err := client.GetFundingCandles(ctx, cur, tf, cfg.PeriodAgg, start, end, limit)
				if err != nil {
					return nil, err
				}
				mts := make([]int64, 0, len(batch))
				for _, c := range batch {
					if err := repo.InsertCandle(ctx, c); err != nil {
						return nil, fmt.Errorf("insert candle: %w", err)
					}
					cs.fetched++
					cs.inserted++
					mts = append(mts, c.MTS)
				}
				logger.Info("page done",
					zap.String("kind", "candles"),
					zap.String("currency", cur),
					zap.String("timeframe", tf),
					zap.Int("batch_size", len(batch)),
					zap.Int64("min_mts", minOrZero(mts)))
				return mts, nil
			}
			if err := paginateBackward(ctx, fetch, startMs, endMs, cfg.BatchSize); err != nil {
				if ctx.Err() != nil {
					return err
				}
				logger.Error("candles backfill failed",
					zap.String("currency", cur),
					zap.String("timeframe", tf),
					zap.Error(err))
				return err
			}
			candleStats = append(candleStats, cs)
		}
	}

	// Stats per currency (only if requested)
	if cfg.AlsoStats {
		for _, cur := range cfg.Currencies {
			ss := statStat{currency: cur}
			fetch := func(ctx context.Context, start, end int64, limit int) ([]int64, error) {
				if limit > 250 {
					limit = 250 // funding/stats endpoint cap
				}
				batch, err := client.GetFundingStats(ctx, cur, start, end, limit)
				if err != nil {
					return nil, err
				}
				mts := make([]int64, 0, len(batch))
				for _, s := range batch {
					if err := repo.InsertStat(ctx, s); err != nil {
						return nil, fmt.Errorf("insert stat: %w", err)
					}
					ss.fetched++
					ss.inserted++
					mts = append(mts, s.MTS)
				}
				logger.Info("page done",
					zap.String("kind", "stats"),
					zap.String("currency", cur),
					zap.Int("batch_size", len(batch)),
					zap.Int64("min_mts", minOrZero(mts)))
				return mts, nil
			}
			if err := paginateBackward(ctx, fetch, startMs, endMs, cfg.BatchSize); err != nil {
				if ctx.Err() != nil {
					return err
				}
				logger.Error("stats backfill failed", zap.String("currency", cur), zap.Error(err))
				return err
			}
			statStats = append(statStats, ss)
		}
	}

	totalElapsed := time.Since(totalStart)
	for _, cs := range candleStats {
		logger.Info("summary candles",
			zap.String("currency", cs.currency),
			zap.String("timeframe", cs.timeframe),
			zap.Int("fetched", cs.fetched),
			zap.Int("inserted", cs.inserted))
	}
	for _, ss := range statStats {
		logger.Info("summary stats",
			zap.String("currency", ss.currency),
			zap.Int("fetched", ss.fetched),
			zap.Int("inserted", ss.inserted))
	}
	logger.Info("backfill complete", zap.Duration("elapsed", totalElapsed))

	if cfg.Verify {
		if err := runVerify(ctx, logger, repo, cfg.Currencies, cfg.Timeframes, cfg.PeriodAgg); err != nil {
			return fmt.Errorf("verify: %w", err)
		}
	}

	_ = domain.FundingCandle{} // ensure domain import is used (the closures use the type indirectly via repo.InsertCandle)
	return nil
}

func minOrZero(xs []int64) int64 {
	if len(xs) == 0 {
		return 0
	}
	m := xs[0]
	for _, x := range xs[1:] {
		if x < m {
			m = x
		}
	}
	return m
}
```

> ⚠️ The `_ = domain.FundingCandle{}` line is a hack to keep the import. If `domain` is referenced in any actual code path (it should be via `repo.InsertCandle(ctx, c)` where `c` is `domain.FundingCandle`), delete that line. Run goimports / `go build` to confirm.

- [ ] **Step 2: Verify build**

Run: `cd backend && go build ./cmd/backfill/`
Expected: no output. Fix any unused imports / type mismatches.

- [ ] **Step 3: Run all tests**

Run: `cd backend && go test ./cmd/backfill/ ./internal/bitfinex/ ./internal/repository/postgres/ -v -timeout 60s`
Expected: all PASS.

- [ ] **Step 4: Commit Group 4 main**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend/cmd/backfill/
git commit -m "✨ Feat: add cmd/backfill runner + verify mode

OpenSpec: bitfinex-funding-historical-backfill (group 4/6)

- main.go: flag parsing (currencies/timeframes CSV, RFC3339 dates),
  pgxpool + bitfinex client setup, signal handling, summary logging
- pagination.go (already committed): paginateBackward pure function
- verify.go: runVerify orchestrates gap detection + FRR unit inference
- expectedIntervalMs lookup for all Bitfinex timeframes
- inferFRRUnit pure function for unit hypothesis (per-second/per-day/per-period)
- 4 + 6 unit tests across pagination + verify

Build target: \`go run ./cmd/backfill -currencies UST,USD -timeframes 1h,1D -start 2023-05-09 -end 2026-05-09 -verify\`"
```

**Checkpoint:** Group 4 committed. End-to-end runnable.

---

## Group 5: Verification & Sanity Check (manual smoke)

**Estimated time:** 20-30 min (10 min compile/test, 10 min smoke, 10 min full backfill)
**Checkpoint commit:** none for code (no code changes); commits land in second-brain wiki update at task 5.6.
**Pitfalls:**
- DATABASE_URL must be sourced — `cd backend && set -a; source ../.env; set +a` then `go run` in same shell.
- First small smoke catches schema-vs-code mismatches (wrong column name, type drift) before burning 7 minutes on the full run.
- The repo's prod & dev share the same Neon DB (per backend/CLAUDE.md). The new tables don't conflict with existing 5, but be aware: deleting funding_candles requires DROP TABLE + atlas migrate down, not a casual TRUNCATE.

**Subagent / parallel opportunity:** None — these steps must be run interactively and observed.

---

### Task 5.1: Compile + test

- [ ] **Step 1: Full build**

Run: `cd backend && go build ./...`
Expected: no output, exit 0.

- [ ] **Step 2: Full test suite**

Run: `cd backend && go test ./... -timeout 90s`
Expected: all PASS. If pre-existing tests fail unrelated to this change, surface and stop — don't push past unrelated regression.

---

### Task 5.2: Lint

- [ ] **Step 1: Run lint per project Makefile**

Run: `cd backend && make lint`
Expected: no output. Fix any new lint findings.

---

### Task 5.3: Smoke backfill (small range)

- [ ] **Step 1: Run smoke**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
set -a; source .env; set +a
cd backend
go run ./cmd/backfill \
  -currencies UST \
  -timeframes 1h \
  -start 2026-05-01 \
  -end 2026-05-09 \
  -verify
```

Expected output:
- Page-done log lines (probably 1 page since 8×24=192 candles fits in batch=10000)
- Summary: ~192 candles inserted
- Stats summary: ~192 stat rows inserted (1/hour for 8 days)
- Verify: "no large gaps" for fUST 1h
- Verify: FRR unit inference line, e.g. `inferred_unit=per-second avg_ratio=169.x`

If FRR unit comes out as `unknown`, look at the actual avg_ratio value and update `inferFRRUnit` thresholds; commit fix; re-run smoke.

- [ ] **Step 2: Verify in DB via psql**

```bash
psql "$DATABASE_URL" -c "SELECT count(*) FROM funding_candles WHERE symbol='fUST' AND timeframe='1h';"
psql "$DATABASE_URL" -c "SELECT count(*) FROM funding_stats WHERE symbol='fUST';"
psql "$DATABASE_URL" -c "SELECT * FROM funding_candles WHERE symbol='fUST' AND timeframe='1h' ORDER BY mts DESC LIMIT 3;"
```

Expected: counts > 100 each, sample rows look sensible (close in 1e-5 to 1e-3 range).

---

### Task 5.4: Idempotency check

- [ ] **Step 1: Re-run same smoke command**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend
go run ./cmd/backfill \
  -currencies UST \
  -timeframes 1h \
  -start 2026-05-01 \
  -end 2026-05-09 \
  -verify
```

- [ ] **Step 2: Confirm no new rows**

Compare counts to Task 5.3 Step 2. Expected: same numbers (ON CONFLICT DO NOTHING worked).

If counts grew, the PK definition is wrong — STOP, debug `funding_candles` schema vs the migration vs `InsertFundingCandle` sqlc params. Likely a column name typo or PK column-list mismatch.

---

### Task 5.5: Full 3-year backfill

> ⚠️ This step takes ~7 minutes and writes ~110K candle rows + ~52K stats rows to Neon. Verify Neon free-tier capacity first (Neon dashboard > Database size) — should be way under 0.5GB.

- [ ] **Step 1: Run**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend
go run ./cmd/backfill \
  -currencies UST,USD \
  -timeframes 1h,1D \
  -start 2023-05-09 \
  -end 2026-05-09 \
  -verify 2>&1 | tee /tmp/bfx-backfill-$(date +%Y%m%d-%H%M).log
```

- [ ] **Step 2: Confirm summary numbers**

Expected (approximate):
- fUST 1h: ~26280 candles
- fUST 1D: ~1095 candles
- fUSD 1h: ~26280 candles
- fUSD 1D: ~1095 candles
- fUST stats: ~26000+ rows (1/hour density assumption)
- fUSD stats: ~26000+ rows
- Verify gap count: should be very small or zero per (currency, tf)
- Verify FRR ratio: should converge to consistent values per currency

- [ ] **Step 3: SQL spot check gaps**

```bash
psql "$DATABASE_URL" <<'SQL'
WITH d AS (
  SELECT symbol, timeframe, count(*) AS n,
         to_timestamp(min(mts)/1000) AS earliest,
         to_timestamp(max(mts)/1000) AS latest
  FROM funding_candles
  GROUP BY symbol, timeframe
)
SELECT * FROM d ORDER BY symbol, timeframe;
SQL
```

Expected: 4 rows (fUST/1h, fUST/1D, fUSD/1h, fUSD/1D). Earliest 2023-05-09 ish, latest near 2026-05-09.

---

### Task 5.6: Update second-brain wiki with FRR finding

- [ ] **Step 1: Capture FRR result from log**

Pull the FRR unit inference lines from `/tmp/bfx-backfill-*.log`. Note the exact ratio + inferred unit per currency.

- [ ] **Step 2: Update wiki**

Edit `~/second-brain/wiki/tech/bitfinex-funding-rate-data-sources.md`. Find the section "FRR 單位待確認 (Stage 1.2 backfill 時驗)" and replace with confirmed answer:

```markdown
### FRR 單位（2026-05-09 backfill 確認）

實測 fUST 5 對 sample 平均 ratio = <X.XX> → **per-<unit>**
fUSD 同樣 → **per-<unit>**

換算：年化 ≈ FRR × <multiplier>
```

(Plug in actual values from log.)

- [ ] **Step 3: Commit wiki update**

```bash
cd ~/second-brain
git add wiki/tech/bitfinex-funding-rate-data-sources.md
git commit -m "wiki: confirm Bitfinex FRR unit via Stage 1.2 backfill (bfx-funding-bot)"
```

**Checkpoint:** Group 5 done. Stage 1.2 data layer is fully populated and validated.

---

## Group 6: Documentation & OpenSpec Closeout

**Estimated time:** 10-15 min
**Checkpoint commits:** wiki update + OpenSpec archive (separate commits in second-brain & bfx repos respectively).
**Pitfalls:**
- `openspec archive` moves the change from `openspec/changes/<name>/` to `openspec/changes/archive/YYYY-MM-DD-<name>/` AND syncs spec files into `openspec/specs/`. If `openspec/specs/` already has overlapping capability folders, archive may merge or conflict — check `openspec list --specs` before archive.
- Project rule: "實作完成後必須確認 `go test ./...` 全部通過才能 archive" — final test run is gating.

---

### Task 6.1: Update second-brain bfx-funding-bot project page

- [ ] **Step 1: Tick off Stage 1.2 sub-tasks in wiki**

Edit `~/second-brain/wiki/projects/bfx-funding-bot.md`. Under "Stage 1.2: 抓 USDT funding 歷史利率落地":
- Mark all 6 sub-checkboxes as `[x]`
- Append a closing line: `→ OpenSpec change [bitfinex-funding-historical-backfill] archived YYYY-MM-DD; data live in Neon (funding_candles ~54k rows, funding_stats ~52k rows); FRR unit confirmed = <unit>`

- [ ] **Step 2: Add Recent Activity entry**

Under `## Recent Activity`:

```markdown
### 2026-05-09

- Stage 1.1 完成：Bitfinex funding 資料來源評估，confirmed 唯一可行路徑是官方 public REST（[[../tech/bitfinex-funding-rate-data-sources]]）
- Stage 1.2 完成：建 funding_candles + funding_stats 兩表（Atlas migration），bitfinex client 加 GetFundingCandles + GetFundingStats + rate-limit backoff retry，cmd/backfill runner 含 --verify mode；3 年 fUST + fUSD × 1h + 1D backfill 完成（~110k rows total），FRR 單位確認為 <unit>
- 走完 OpenSpec + superpowers brainstorming + writing-plans 完整流程
```

- [ ] **Step 3: Commit**

```bash
cd ~/second-brain
git add wiki/projects/bfx-funding-bot.md
git commit -m "wiki: bfx-funding-bot Stage 1.2 完成 — funding 歷史資料層落地"
```

---

### Task 6.2: Final test + OpenSpec validation

- [ ] **Step 1: Full test pass (gate)**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend
go test ./...
```
Expected: all PASS. If fail → fix before archive.

- [ ] **Step 2: OpenSpec validate**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
openspec validate bitfinex-funding-historical-backfill
```
Expected: `is valid`.

- [ ] **Step 3: Tick all tasks.md checkboxes**

Edit `openspec/changes/bitfinex-funding-historical-backfill/tasks.md`. Tick every remaining `- [ ]` to `- [x]`. (Group 1 already ticked.)

```bash
git add openspec/changes/bitfinex-funding-historical-backfill/tasks.md
git commit -m "📝 Docs: tick all tasks for bitfinex-funding-historical-backfill change"
```

---

### Task 6.3: OpenSpec archive

- [ ] **Step 1: Pre-archive sanity**

```bash
openspec list --specs 2>&1 | grep -i funding
```

If existing capability folders (`funding-history-storage`, `bitfinex-public-market-data`, `funding-data-backfill`) already exist in `openspec/specs/`, expect archive to perform a merge — be ready to inspect output.

- [ ] **Step 2: Archive**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
openspec archive bitfinex-funding-historical-backfill
```

Expected: confirmation that change moved + specs synced to `openspec/specs/<capability>/spec.md`.

- [ ] **Step 3: Commit archive**

```bash
git add openspec/changes/archive/ openspec/specs/ openspec/changes/bitfinex-funding-historical-backfill 2>/dev/null
git status   # verify the change folder is gone, archive + specs/ added
git commit -m "📝 Docs: archive bitfinex-funding-historical-backfill OpenSpec change

Stage 1.2 data layer complete. Specs synced to openspec/specs/:
- funding-history-storage
- bitfinex-public-market-data
- funding-data-backfill

Stage 1.3 (backtest engine) unblocked. See ~/second-brain/wiki/projects/bfx-funding-bot.md."
```

**Checkpoint:** All Group 6 done. Change is archived, wiki current, Stage 1.3 unblocked.

---

## Self-Review

**Spec coverage check** (skim every Requirement → which task implements it):

| Spec | Requirement | Implementing Task |
|---|---|---|
| funding-history-storage | persist candles | 1.1-1.4 (schema, done) + 3.1-3.2 (insert query/wrapper) |
| funding-history-storage | persist stats | same |
| funding-history-storage | indexed time-range queries | 1.1-1.4 (mts indexes already in schema) |
| funding-history-storage | typed Go query API via sqlc | 3.1-3.2 |
| bitfinex-public-market-data | fetch candles | 2.5 |
| bitfinex-public-market-data | fetch stats | 2.6 |
| bitfinex-public-market-data | respect rate limits | 2.7 (burst clamping test) |
| bitfinex-public-market-data | retry rate-limit with backoff | 2.4 (3 backoff tests) |
| bitfinex-public-market-data | configurable backoff base | 2.2 (option) + 2.4 tests |
| bitfinex-public-market-data | no auth credentials | 2.4 (doPublicGET signature) |
| funding-data-backfill | invokable as standalone command | 4.3 |
| funding-data-backfill | fetch + persist candles | 4.3 |
| funding-data-backfill | persist funding stats | 4.3 |
| funding-data-backfill | respect rate limits + report progress | 4.3 (per-page log) |
| funding-data-backfill | handle empty/out-of-range | 4.1 (paginateBackward) + 4.3 |
| funding-data-backfill | pagination cursor as pure function | 4.1 |
| funding-data-backfill | --verify mode | 4.2 + 4.3 + 5.5 |

✓ All 17 requirements covered.

**Placeholder scan:** None — every step has actual code.

**Type consistency:**
- `paginateBackward(ctx, fetch, startMs, endMs, batchSize)` — used identically in 4.3 closures and 4.1 tests ✓
- `FundingMarketDataRepo` methods (`InsertCandle` / `InsertStat` / `CountCandles` / `CountStats` / `GetEarliestCandleMTS` / `DetectCandleGaps` / `SampleStatCandlePairs`) — used in 4.3 main and 4.2 verify; signatures consistent ✓
- `domain.FundingCandle` / `domain.FundingStat` field names — used in 2.x client methods + 3.2 wrapper + 4.3 main; consistent ✓
- `inferFRRUnit(ratio float64) string` — defined 4.2, used 4.2; consistent ✓

**Scope check:** 6 groups / 27 atomic tasks / ~3-4 hours total. Single implementation cycle. Within bite-sized constraint.

---

## Implementation Sequence Recommendation

**Hard ordering:**
- Group 1 → 2 → 3 → 4 → 5 → 6 (each group depends on prior)

**Within Group 2:** 2.1 → 2.2 → 2.3 → 2.4 → 2.5 → 2.6 → 2.7 → 2.8 (commit). Tests interleaved with implementation per TDD.

**Within Group 4:** 4.1 (commit) → 4.2 → 4.3 (commit). 4.1 is self-contained and testable; 4.2 + 4.3 share the verify infrastructure.

**Subagent strategy (if subagent-driven-development chosen):**
- Group 2 = 1 subagent (cohesive client refactor + tests)
- Group 3 = 1 subagent (SQL + sqlc + repo)
- Group 4 task 4.1 = 1 subagent (small isolated)
- Group 4 tasks 4.2-4.3 = 1 subagent
- Group 5 = supervised (manual smoke needs human eyes)
- Group 6 = supervised (archive + wiki update touch second-brain)

Total: 4 fresh subagents + 2 supervised phases.

---

**Stage 1.3 unblocking note:** After this plan completes, the backtest engine (Stage 1.3) can begin. It will need to consume `funding_candles` and `funding_stats` via the sqlc-generated query layer — extend `query/funding.sql` with read queries (`ListCandlesByRange`, `ListStatsByRange`) when starting Stage 1.3.
