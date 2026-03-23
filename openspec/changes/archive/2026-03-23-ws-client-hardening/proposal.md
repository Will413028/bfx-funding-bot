## Why

前端 WebSocket 相關程式碼有三個已知問題：

1. **WS Store race condition** — `connecting` flag 宣告在 Zustand store 外部（module-level variable），多個元件快速呼叫 `connect()` 時可能繞過 guard
2. **WS token 過期未處理** — WebSocket 連線建立後，若 token TTL 到期（15 分鐘），不會自動重新取得 token 並重連
3. **Proxy token refresh 並發問題** — 多個 API request 同時收到 401 時，各自獨立觸發 token refresh，造成不必要的多次 refresh

## What Changes

- 將 `connecting` flag 移入 Zustand state，確保狀態一致性
- WS client 追蹤 token TTL，在到期前主動重連
- Proxy route 使用 deduplication pattern（shared promise），合併並發的 refresh request

## Capabilities

### New Capabilities

（無——強化現有功能的可靠性）

### Modified Capabilities

- `ws-store`: 連線狀態管理更安全
- `ws-client`: 支援 token 到期前自動重連
- `api-proxy`: Token refresh 請求去重

## Impact

- `frontend/src/stores/ws-store.ts` — connecting flag 移入 state
- `frontend/src/lib/ws-client.ts` — token TTL 追蹤 + 重連邏輯
- `frontend/src/app/api/proxy/[...path]/route.ts` — refresh deduplication
