## Why

UserService 有 9 個公開方法，但只有前 4 個（Register, Login, GetProfile, ChangePassword）有測試覆蓋。Token 相關流程（VerifyEmail, RequestPasswordReset, ResetPassword, RefreshToken, Logout）完全沒有測試，這些是安全關鍵路徑——token 驗證、密碼重設、refresh rotation 的正確性直接影響帳戶安全。AuthHandler 也完全沒有測試檔案。

## What Changes

- 補齊 `service/user_test.go` 中 5 個未測試方法的 unit tests（VerifyEmail, RequestPasswordReset, ResetPassword, RefreshToken, Logout）
- 新增 `handler/auth_test.go`，覆蓋所有 8 個 auth handler endpoints（Register, Login, Refresh, Logout, VerifyEmail, ForgotPassword, ResetPassword）
- 複用現有 mock 基礎設施（mockUserRepo, mockTokenRepo, mockNotifier, testJWTManager）

## Capabilities

### New Capabilities

- `auth-token-flow-tests`: UserService token 流程（verify email、password reset、refresh token rotation、logout）的 service 層 + handler 層測試覆蓋

### Modified Capabilities

（無——只補測試，不改變行為）

## Impact

- `backend/internal/service/user_test.go` — 新增 ~10 個 test functions
- `backend/internal/handler/auth_test.go` — 新增檔案，~15 個 test functions
- 不影響任何 production code、API 或 DB schema
