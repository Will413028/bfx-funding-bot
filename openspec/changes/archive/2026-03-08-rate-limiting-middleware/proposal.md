## Why

目前所有 API endpoint 沒有請求頻率限制，任何人可以無限制地打 API。這會導致：
1. 惡意使用者或爬蟲耗盡伺服器資源
2. Bitfinex API 配額被間接消耗（透過 dashboard/earnings 等 proxy 呼叫）
3. 無法區分正常用戶和異常流量

Rate limiting 是 API 上線前的基本防護，需要在更多功能開發前完成。

## What Changes

- 新增 `middleware/ratelimit.go`：基於 IP 的請求頻率限制 middleware
- 使用 Go 內建的 `golang.org/x/time/rate` token bucket 演算法，無需外部依賴
- 全局套用至所有 `/api/v1` 路由
- 超過限制時回傳 `429 Too Many Requests`，附帶 `Retry-After` header
- 公開路由（auth/register, auth/login）和受保護路由使用不同的限制策略
- Health check endpoint 不受限制

## Capabilities

### New Capabilities

- `api-rate-limiting`: 基於 IP 的 API 請求頻率限制，包含 token bucket 演算法、不同路由群組的差異化限制、429 回應格式

### Modified Capabilities

（無既有 spec 需要修改）

## Impact

- **程式碼**：`middleware/ratelimit.go`（新增）、`handler/router.go`（加入 middleware）
- **依賴**：`golang.org/x/time/rate`（新增）
- **API**：所有 endpoint 加上頻率限制，超限回傳 429
- **回應 Header**：新增 `X-RateLimit-Limit`、`X-RateLimit-Remaining`、`Retry-After`
