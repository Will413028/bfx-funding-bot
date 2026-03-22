## 1. Service 層測試 — token 流程

- [x] 1.1 新增 TestVerifyEmail_Success：手動注入 verify token → 呼叫 VerifyEmail → 驗證 user status 變為 active
- [x] 1.2 新增 TestVerifyEmail_InvalidToken：呼叫 VerifyEmail 帶不存在的 token → 驗證回傳 INVALID_OR_EXPIRED_TOKEN
- [x] 1.3 新增 TestRequestPasswordReset_ExistingEmail：呼叫 RequestPasswordReset 帶已存在 email → 驗證回傳 nil
- [x] 1.4 新增 TestRequestPasswordReset_NonExistentEmail：呼叫 RequestPasswordReset 帶不存在 email → 驗證回傳 nil（防列舉）
- [x] 1.5 新增 TestResetPassword_Success：注入 reset token → 呼叫 ResetPassword → 驗證密碼已更新
- [x] 1.6 新增 TestResetPassword_InvalidToken：呼叫 ResetPassword 帶無效 token → 驗證回傳 INVALID_OR_EXPIRED_TOKEN
- [x] 1.7 新增 TestResetPassword_PasswordTooShort：呼叫 ResetPassword 帶 < 8 字元密碼 → 驗證回傳 PASSWORD_TOO_SHORT
- [x] 1.8 新增 TestResetPassword_PasswordTooLong：呼叫 ResetPassword 帶 > 72 字元密碼 → 驗證回傳 PASSWORD_TOO_LONG
- [x] 1.9 新增 TestRefreshToken_Success：注入 refresh token + user → 呼叫 RefreshToken → 驗證回傳新 token pair、舊 token 已刪除
- [x] 1.10 新增 TestRefreshToken_InvalidToken：呼叫 RefreshToken 帶無效 token → 驗證回傳 INVALID_OR_EXPIRED_TOKEN
- [x] 1.11 新增 TestLogout_Success：呼叫 Logout → 驗證回傳 nil

## 2. Handler 層測試 — auth endpoints

- [x] 2.1 新增 `handler/auth_test.go`，建立 test helper（setupAuthRouter + mock service 依賴）
- [x] 2.2 新增 TestAuthHandler_Register_Success + TestAuthHandler_Register_ValidationError
- [x] 2.3 新增 TestAuthHandler_Login_Success + TestAuthHandler_Login_WrongPassword
- [x] 2.4 新增 TestAuthHandler_Refresh_Success + TestAuthHandler_Refresh_InvalidToken
- [x] 2.5 新增 TestAuthHandler_Logout_Success
- [x] 2.6 新增 TestAuthHandler_VerifyEmail_Success + TestAuthHandler_VerifyEmail_InvalidToken
- [x] 2.7 新增 TestAuthHandler_ForgotPassword_Always200
- [x] 2.8 新增 TestAuthHandler_ResetPassword_Success + TestAuthHandler_ResetPassword_InvalidToken

## 3. 驗證

- [x] 3.1 `cd backend && go build ./...` 通過
- [x] 3.2 `cd backend && go test ./...` 全部通過
