## Why

WebSocket Hub 的 `eventLoop` 中有三條路徑會呼叫 `close(c.send)`：

1. **shutdown**（line ~154）— 遍歷所有 clients 並 close
2. **unregister**（line ~166）— 從 map 刪除後 close
3. **slow-client**（line ~177）— 從 map 刪除後 close

如果 shutdown 與 unregister/slow-client 同時發生（例如 server 正在 graceful shutdown，同時有 client 斷線觸發 unregister），同一個 channel 會被 close 兩次，造成 **panic: close of closed channel**。

在高併發環境下（多用戶同時連線的 Dashboard），這個 race condition 有實際觸發風險。

## What Changes

- 為 WebSocket client 加入 close-once guard（`sync.Once` 或 map-check），確保 `send` channel 只會被 close 一次
- 新增測試驗證並行 shutdown + unregister 不會 panic

## Capabilities

### New Capabilities

（無——純 bug fix）

### Modified Capabilities

- `ws-hub`: WebSocket Hub 的 client 生命週期管理更安全

## Impact

- `backend/internal/handler/ws.go` — eventLoop 中的 close 邏輯
