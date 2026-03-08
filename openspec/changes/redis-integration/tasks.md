## 1. Dependencies

- [x] 1.1 Add `github.com/redis/go-redis/v9`: `cd backend && go get github.com/redis/go-redis/v9`

## 2. Config

- [x] 2.1 Add `RedisURL` field to `appconfig/config.go` and load from env var (required)

## 3. Infrastructure

- [x] 3.1 Create `infra/redis.go`: parse URL, create `*redis.Client`, PING to verify, register fx Lifecycle close hook
- [x] 3.2 Update `infra/di.go`: add Redis provider to Module

## 4. Health Check

- [x] 4.1 Update `handler/health.go`: inject `*redis.Client`, add Redis PING to health response, report `"degraded"` if Redis fails

## 5. Tests

- [x] 5.1 All existing tests pass (health handler tested via handler test suite)
