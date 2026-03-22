## Context

`service/user_test.go` 已有完整的 mock 基礎設施（mockUserRepo, mockTokenRepo, mockNotifier, testJWTManager），覆蓋 Register/Login/GetProfile/ChangePassword。Token 相關的 5 個方法（VerifyEmail, RequestPasswordReset, ResetPassword, RefreshToken, Logout）使用相同的依賴，但完全沒有測試。

`handler/auth.go` 定義了 8 個 endpoint，但沒有 `auth_test.go`。相鄰的 `handler/user_test.go` 已展示測試模式（gin.TestMode + httptest + mock service）。

## Goals / Non-Goals

**Goals:**
- 補齊 service 層 5 個方法的 unit tests，覆蓋 success + error paths
- 新增 handler 層 auth_test.go，覆蓋所有 7 個 endpoint（Logout 是靜態回應，合併測試）
- 複用現有 mock，不引入新依賴

**Non-Goals:**
- 不改 production code
- 不做 integration test（不連 DB/Redis）
- 不測 handleError（已被其他 handler test 間接覆蓋）

## Decisions

**1. Service 測試策略：直接利用 mockTokenRepo 的 Store → Get 循環**

token 流程的核心是 `generateToken()` → `hashToken()` → Redis Store/Get。由於 `generateToken` 是私有函式，測試無法直接控制 token 值。方案：

- 先呼叫 Register（內部會 Store verify token），再從 mockTokenRepo 取出 stored hash 來測試 VerifyEmail ❌ — mock 不追蹤 token hash
- **改用 indirect testing**：呼叫方法後驗證 side effects（user status 變更、password 變更、token 刪除）✅

對 RefreshToken：呼叫 Login 取得 refreshToken，再傳入 RefreshToken 方法。但 Login 回傳的 `refreshToken` 是 plaintext，service 內部存的是 hash。解法：**在 test 中手動向 mockTokenRepo 注入 token mapping**，模擬「已存在的 valid token」。

**2. Handler 測試：複用 UserService 搭配 mock repos**

Handler 直接依賴 `*service.UserService`（concrete type），不是 interface。因此 handler test 需要：
- 建立完整的 `service.UserService`（注入 mock repos）
- 用 `httptest.NewRecorder` + gin engine 發送 HTTP 請求

這與 `handler/user_test.go` 現有模式一致。

## Risks / Trade-offs

- **Token hash 不可控**：`generateToken()` 用 `crypto/rand`，無法預測。測試改為手動注入 mockTokenRepo entries，繞過 token 生成。trade-off 是測試不覆蓋 `generateToken` + `hashToken` 本身，但這兩個函式邏輯極簡（rand + sha256）。
- **Logout 是空實作**：目前 `Logout()` 直接 return nil，handler 也不呼叫 service。測試只驗證 HTTP 200 回應，覆蓋意義有限但確保未來實作不破壞 contract。
