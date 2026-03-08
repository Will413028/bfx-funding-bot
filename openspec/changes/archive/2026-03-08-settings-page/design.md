## Context

Settings 是 Dashboard 最後一個核心頁面。使用者需要查看帳戶資訊（email、plan、狀態）和修改密碼。後端已提供 GET /me 和 PUT /me/password API。頁面結構簡單：兩張卡片（Profile + Change Password），不需要複雜佈局。

## Goals / Non-Goals

**Goals:**
- ProfileCard 顯示帳戶資訊（email、status badge、plan badge、註冊日期）
- ChangePasswordForm 修改密碼（current + new + confirm，Zod 驗證 match）
- premium dark theme 卡片風格
- 成功/失敗回饋

**Non-Goals:**
- 不實作頭像上傳
- 不實作 email 修改（後端不支援）
- 不實作帳戶刪除
- 不實作方案升級（未來 Billing 頁面處理）

## Decisions

### D1: 不新增 query key factory

GET /me 用 inline query key `["user", "me"]`，因為目前只有一個 user endpoint，不需要額外的 key factory。

### D2: 密碼表單獨立 Zod schema

新增 `changePasswordSchema`：
- currentPassword: required
- newPassword: required, min 8 chars
- confirmPassword: required
- 跨欄位 refine: newPassword === confirmPassword

### D3: 成功回饋用 mutation.isSuccess

密碼修改成功後顯示 "Password changed!" 文字（與 F8 Strategy 的 Save 回饋一致），不需要 toast 系統。

## Risks / Trade-offs

- [無 toast 系統] → 暫時用 mutation state 顯示回饋，F15 可考慮加入 sonner
