## Why

目前 JWT access token 有效期為 24 小時，前端 cookie maxAge 設 7 天，兩者不一致。Token 過期後用戶必須重新輸入帳密登入，體驗差。更重要的是，長效 access token 被盜後攻擊者可長時間使用。業界最佳做法是短期 access token + 長期 refresh token rotation，兼顧安全性與使用體驗。

## What Changes

- Access token 過期時間從 24 小時縮短為 **15 分鐘**
- 新增 refresh token（opaque、64 bytes random），儲存於 Redis（7 天 TTL）
- Login 同時回傳 access token + 設定 refresh token httpOnly cookie
- 新增 `POST /api/v1/auth/refresh` endpoint — 用 refresh token 換新的 access + refresh token pair（rotation）
- 前端 API proxy 自動偵測 401 → 嘗試 refresh → 重試原始請求（透明刷新）
- Logout 刪除 Redis 中的 refresh token（server-side revocation）
- 前端 cookie 拆分為兩個：`auth_token`（access, 15min）+ `refresh_token`（refresh, 7d, httpOnly）

## Capabilities

### New Capabilities
- `refresh-token-backend`: 後端 refresh token 生命週期 — 生成、Redis 儲存、rotation、revocation、refresh endpoint
- `refresh-token-frontend`: 前端自動 refresh 機制 — cookie 管理、401 攔截、透明重試

### Modified Capabilities

## Impact

- `auth/jwt.go` — access token 過期時間縮短為 15 分鐘
- `handler/auth.go` — Login 回傳 refresh token、新增 Refresh + Logout endpoint
- `service/user.go` — Login/Refresh/Logout 邏輯
- `repository/redis/` — 新增 refresh token 儲存
- `repository/interfaces.go` — 擴展 TokenRepository 或新增 RefreshTokenRepository
- `frontend/src/app/[locale]/(auth)/actions.ts` — 設定雙 cookie
- `frontend/src/app/api/proxy/[...path]/route.ts` — 401 自動 refresh + 重試
- `frontend/middleware.ts` — 調整 token 過期判斷邏輯
