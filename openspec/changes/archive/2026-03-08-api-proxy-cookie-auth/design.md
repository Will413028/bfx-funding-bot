## Context

前端 `api-client.ts` Client Component 請求會打 `/api/proxy/*`，需要一個 catch-all API Route 將請求轉發至 Go 後端。登入流程需要 Server Action 處理 JWT → HttpOnly cookie 的轉換，確保 token 不暴露給瀏覽器 JS。

Go 後端登入 endpoint `POST /api/v1/auth/login` 回傳 `{ "data": { "token": "jwt..." } }`。

## Goals / Non-Goals

**Goals:**
- 實作通用同源代理，支援所有 HTTP methods
- 登入成功後以 HttpOnly cookie 安全儲存 JWT
- 登出時清除 cookie
- 註冊成功後自動登入（設 cookie）

**Non-Goals:**
- 不實作 login/register UI 頁面（F4 範疇）
- 不實作 token refresh / rotation（MVP 階段 token 7 天過期即重新登入）
- 不實作 WS token route（F12 範疇）

## Decisions

### D1: Proxy 用 catch-all API Route

`app/api/proxy/[...path]/route.ts` 一個檔案處理所有代理請求。路徑直接映射：`/api/proxy/api-keys` → `/api/v1/api-keys`。

**替代方案**：每個 endpoint 各寫一個 route → 大量重複程式碼，違反 DRY。

### D2: Login 用 Server Action 而非 Proxy

登入不走 proxy route，改用 Server Action。原因：proxy 只是透傳，無法在回應中設定 cookie。Server Action 可以呼叫 `cookies().set()` 後再回傳結果給 client。

**替代方案**：proxy route 攔截 `/auth/login` 路徑特殊處理 → 混雜了 proxy 的職責，不乾淨。

### D3: Cookie 安全旗標

| 旗標 | 值 | 理由 |
|------|------|------|
| HttpOnly | true | 防 XSS 讀取 |
| Secure | production only | localhost 開發需 HTTP |
| SameSite | Lax | 防 CSRF + 允許導航帶 cookie |
| Path | / | 全站可用 |
| Max-Age | 604800 | 7 天 |

### D4: Register 自動登入

Register Server Action 先呼叫後端 `/auth/register`，成功後再呼叫 `/auth/login` 取得 token 設 cookie。一步完成，使用者不需重複輸入密碼。

## Risks / Trade-offs

- [Token 無法提前撤銷] → MVP 階段可接受，後續可加 token blacklist 或改用短期 token + refresh token
- [Proxy 不驗證 path] → 信任前端 api-client 只打合法路徑，後端本身有 auth middleware 把關
