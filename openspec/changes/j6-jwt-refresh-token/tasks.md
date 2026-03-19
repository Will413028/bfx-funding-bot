## 1. Backend: Access Token Expiry

- [x] 1.1 Change `tokenExpiry` in `auth/jwt.go` from 24h to 15 minutes
- [x] 1.2 Update auth tests that depend on token expiry timing

## 2. Backend: Refresh Token Service

- [x] 2.1 Add `RefreshToken(ctx, refreshToken) → (accessToken, newRefreshToken, expiresAt, error)` to `service/user.go`
- [x] 2.2 Add `Logout(ctx, userID)` to `service/user.go` — delete refresh token from Redis
- [x] 2.3 Update `Login()` to return refresh token alongside access token (generate + store in Redis)
- [x] 2.4 Write tests: refresh happy path, refresh with invalid token, refresh with expired token, logout

## 3. Backend: Handler + Routes

- [x] 3.1 Add `POST /api/v1/auth/refresh` handler — accepts `{ refreshToken }`, returns `{ accessToken, refreshToken, expiresAt }`
- [x] 3.2 Add `POST /api/v1/auth/logout` handler (protected, JWT required) — calls `service.Logout(userID)`
- [x] 3.3 Wire routes in `router.go`: `/auth/refresh` (public, rate limited), `/auth/logout` (protected)
- [x] 3.4 Update Login handler to include `refreshToken` in response

## 4. Frontend: Cookie Management

- [x] 4.1 Update `setAuthCookie()` in `actions.ts` to set dual cookies: `auth_token` (15min) + `refresh_token` (7d, path=/api, sameSite=strict)
- [x] 4.2 Update `logout()` to delete both cookies + call backend `POST /auth/logout`
- [x] 4.3 Update `register()` — after auto-login, also set refresh_token cookie

## 5. Frontend: Transparent Refresh in Proxy

- [x] 5.1 Update `/api/proxy/[...path]/route.ts` — on 401, attempt refresh using `refresh_token` cookie
- [x] 5.2 Implement refresh logic: call backend `/auth/refresh` → set new cookies → retry original request
- [x] 5.3 Handle refresh failure: clear cookies, return 401

## 6. Frontend: Middleware Update

- [x] 6.1 Update `middleware.ts` — allow access to protected routes if `refresh_token` exists (even if `auth_token` expired)
- [x] 6.2 Only redirect to login if neither cookie exists

## 7. Verification

- [x] 7.1 Run backend tests (`go test ./... -race`) — all pass
- [x] 7.2 Run golangci-lint — no new warnings
- [x] 7.3 Run frontend lint + type check (`pnpm lint`)
- [x] 7.4 Update ROADMAP.md — mark J6 complete, mark J4 as not applicable
