## Context

目前架構：
- Backend `auth/jwt.go`: RS256 JWT, 24hr 過期
- Frontend `actions.ts`: Login 後設定 `auth_token` httpOnly cookie (maxAge 7d)
- Frontend `middleware.ts`: 讀 cookie 解碼 JWT 檢查 `exp`
- API Proxy: 讀 `auth_token` cookie → `Authorization: Bearer` header → backend

目標：access token 15min + refresh token 7d rotation。

## Goals / Non-Goals

**Goals:**
- Access token 過期時間縮短為 15 分鐘（降低被盜風險）
- Refresh token 儲存在 Redis（server-side revocation）
- Token rotation：每次 refresh 產生新的 refresh token，舊的立即失效
- 前端透明刷新（用戶無感知）
- Logout 即時失效（刪除 Redis 中的 refresh token）

**Non-Goals:**
- Refresh token family detection（偵測被盜的 refresh token chain）— 可未來加
- 多裝置 session 管理 UI
- 從 Bearer token 切換到 cookie-only auth（backend 維持 Authorization header 模式）

## Decisions

### 1. Refresh token 格式：opaque random string，不是 JWT

**選擇**：64 bytes `crypto/rand` → hex encode（128 字元），SHA-256 hash 存 Redis。

**替代**：用 JWT 做 refresh token。但 JWT 是自包含的，無法 server-side revoke（除非也查 Redis），失去 JWT 的唯一優勢（stateless）。Opaque token + Redis 查詢更簡單直接。

### 2. 複用現有 TokenRepository（Redis）

**選擇**：使用已有的 `repository.TokenRepository` interface（Store/Get/Delete），tokenType="refresh"。不新增 interface。

**理由**：TokenRepository 已支援 TTL + type 區分，refresh token 的需求與 verify/reset token 相同（存 hash → 查 userID → 刪除）。

### 3. 前端雙 cookie 策略

**選擇**：
- `auth_token`（access token）— httpOnly, secure, sameSite=lax, maxAge=15min
- `refresh_token`（refresh token plaintext）— httpOnly, secure, sameSite=strict, path=/api, maxAge=7d

**`refresh_token` 用 SameSite=Strict + path=/api**：只有同源的 API 請求會帶，進一步縮小暴露面。

### 4. 透明刷新在 API Proxy 層

**選擇**：`/api/proxy/[...path]/route.ts` 收到 401 → 呼叫 backend `/auth/refresh` → 設定新 cookies → 重試原請求。

**替代**：在 `api-client.ts` 做 retry。但 client-side 無法設定 httpOnly cookie，refresh 必須在 server-side（Next.js API route 或 server action）完成。

### 5. Backend Refresh endpoint 設定 refresh token cookie

**選擇**：Backend `POST /auth/refresh` 回傳 `{ accessToken, refreshToken }`。前端 proxy 負責設定 cookies。

**替代**：Backend 直接設 Set-Cookie。但 backend 和 browser 之間隔了 Next.js proxy，backend 設的 cookie domain/path 不對。由 proxy 統一管理 cookie 更乾淨。

## Risks / Trade-offs

- **[Risk] 15 分鐘內 access token 仍可被用** → 這是 JWT 的固有限制，可接受
- **[Risk] Redis 故障 → 無法 refresh** → refresh 失敗時 fallback 到要求重新登入，與目前行為一致
- **[Trade-off] 每 15 分鐘多一次 refresh 請求** → 對 Redis 負載微乎其微，換來大幅安全提升
