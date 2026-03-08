## 1. Dependencies

- [x] 1.1 Add `golang.org/x/time` dependency: `cd backend && go get golang.org/x/time`

## 2. Core Implementation

- [x] 2.1 Create `middleware/ratelimit.go`: implement `IPRateLimiter` struct with `sync.Map` storage, per-IP `rate.Limiter` instances, configurable rate/burst
- [x] 2.2 Implement `RateLimit(limit rate.Limit, burst int) gin.HandlerFunc`: extract client IP, get-or-create limiter, check `Allow()`, set response headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`), return 429 with `Retry-After` on rejection
- [x] 2.3 Implement background cleanup goroutine: track last-seen timestamp per IP, remove entries idle > 10 minutes, run every 5 minutes, stop via context cancellation

## 3. Router Integration

- [x] 3.1 Update `handler/router.go`: apply `RateLimit(5, 10)` to auth route group, apply `RateLimit(20, 40)` to protected route group, keep health check outside rate-limited groups

## 4. Tests

- [x] 4.1 Create `middleware/ratelimit_test.go`: test requests within limit pass with correct headers, test requests exceeding burst return 429 with `Retry-After`, test different IPs have independent limiters, test cleanup removes idle entries
