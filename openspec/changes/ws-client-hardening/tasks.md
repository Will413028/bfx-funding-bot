## 1. WS Store — connecting flag 修正

- [x] 1.1 讀取 `stores/ws-store.ts` 現有結構
- [x] 1.2 將 module-level `connecting` 變數移入 Zustand state
- [x] 1.3 更新 `connect()` 用 `get().connecting` / `disconnect()` 用 `set({ connecting: false })`

## 2. WS Client — token 到期重連

- [x] 2.1 讀取 `lib/ws-client.ts` 現有 token fetch 邏輯
- [x] 2.2 新增 `tokenRefreshTimer` 欄位 + `DEFAULT_TOKEN_TTL_MS`（15 分鐘）
- [x] 2.3 `connect()` 成功後呼叫 `scheduleTokenRefresh()`：在 TTL - 2 分鐘時 close WS 觸發重連
- [x] 2.4 `disconnect()` 和 `onclose` 清理 tokenRefreshTimer

## 3. Proxy — refresh deduplication

- [x] 3.1 讀取 `api/proxy/[...path]/route.ts` 現有 refresh 邏輯
- [x] 3.2 新增 module-level `refreshPromise` 變數 + `tryRefreshDedup()` wrapper
- [x] 3.3 `tryRefresh()` 改回傳新 access token（而非 boolean），供 retry 直接使用
- [x] 3.4 `doFetch()` 新增 `overrideToken` 參數，retry 時繞過 cookie 讀取

## 4. 驗證

- [x] 4.1 `biome check` 通過
