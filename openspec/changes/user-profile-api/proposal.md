## Why

目前用戶完成註冊/登入後，沒有 API 可以查詢自己的 profile 或修改密碼。前端儀表板需要顯示用戶資訊（email、帳號狀態、建立時間），也需要基本的帳號管理功能（改密碼）。這是前端開發前的必備 API。

## What Changes

- 新增 `handler/user.go`：用戶 profile 相關的 HTTP handler
- 擴充 `service/user.go`：新增 GetProfile、ChangePassword 方法
- 新增 API endpoints：
  - `GET /api/v1/me` — 查詢當前用戶 profile
  - `PUT /api/v1/me/password` — 修改密碼
- 更新 `handler/router.go`：註冊新路由

## Capabilities

### New Capabilities

- `user-profile`: 用戶 profile 查詢與帳號管理（查詢 profile、修改密碼）

### Modified Capabilities

（無既有 spec 需要修改）

## Impact

- **程式碼**：`handler/user.go`（新增）、`service/user.go`（擴充）、`handler/router.go`（新路由）
- **API**：新增 2 個 protected endpoints
- **依賴**：無新依賴，使用現有的 bcrypt + repository
