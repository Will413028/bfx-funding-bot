## Context

目前 `UserService` 只有 Register 和 Login。`UserRepository` 有 GetByID 但 service 層沒有暴露 profile 查詢。JWT middleware 已將 `user_id` 存入 Gin context，handler 可直接取用。

## Goals / Non-Goals

**Goals:**
- GET /api/v1/me — 用 JWT 中的 user_id 查詢 profile（不回傳 password_hash）
- PUT /api/v1/me/password — 驗證舊密碼後更新為新密碼

**Non-Goals:**
- 修改 email（涉及驗證流程，未來再做）
- 管理員停用/啟用帳號（admin API，未來再做）
- 頭像上傳

## Decisions

### 1. 路由設計：`/me` 而非 `/users/:id`

使用 `/me` 表示「當前登入用戶」，避免 IDOR 風險（用戶嘗試存取其他人的 profile）。user_id 從 JWT context 取得，不需要額外的權限檢查。

### 2. 密碼修改：要求舊密碼

修改密碼時必須提供 current_password 做二次驗證，防止 token 被盜用後直接改密碼。

### 3. 不新增 repository 方法

- GetProfile 使用既有的 `repo.GetByID()`
- ChangePassword 需要新增 `repo.UpdatePassword()` — 只更新 password_hash 和 updated_at

## Risks / Trade-offs

- **[取捨] /me vs /users/:id** — /me 比較安全簡單，但未來需要 admin API 時得另外加 /users/:id 路由。目前階段 /me 足夠。
