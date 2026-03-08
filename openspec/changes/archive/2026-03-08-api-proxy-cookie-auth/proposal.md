## Why

F2 建立了 `api-client.ts`，Client Component 的請求會打 `/api/proxy` 前綴，但這個 API Route 還不存在。同時前端缺少登入/登出機制——需要 Server Action 與 Go 後端互動，取得 JWT 後以 HttpOnly cookie 儲存，讓同源代理和 middleware 能讀取 token。這是 F4 Auth Pages 的前置條件。

## What Changes

- 新增 `src/app/api/proxy/[...path]/route.ts` — 通用同源代理 API Route，從 HttpOnly cookie 讀 `auth_token`，轉為 `Authorization: Bearer` header 轉發至 Go 後端，支援 GET/POST/PUT/DELETE
- 新增 `src/app/[locale]/(auth)/actions.ts` — login Server Action（呼叫後端 `/auth/login`，成功設 HttpOnly cookie）+ register Server Action + logout Server Action（清除 cookie）
- 登入代理需特殊處理：後端回傳 JWT 時，proxy 不直接回傳 token 給 client，改由 Server Action 設定 HttpOnly cookie

## Capabilities

### New Capabilities
- `api-proxy`: 同源代理 API Route，cookie→Bearer 轉換，查詢參數保留，錯誤透傳
- `cookie-auth`: Server Action 登入/登出，HttpOnly cookie 管理

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/app/api/proxy/[...path]/route.ts` — 新增
- `frontend/src/app/[locale]/(auth)/actions.ts` — 新增
- 依賴 `API_URL` 環境變數（已在 `.env.example` 定義）
- 依賴 `AUTH_SECRET` 環境變數（cookie 簽名用）
- middleware.ts 已在 F1 實作 auth_token cookie 檢查，無需修改
