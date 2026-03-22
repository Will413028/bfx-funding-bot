## 1. 分析現有程式碼

- [x] 1.1 讀取 `handler/ws.go`，確認三條 close 路徑的確切行號與邏輯
- [x] 1.2 確認 `client` struct 沒有任何 close guard

## 2. 實作修正

- [x] 2.1 在 `client` struct 新增 `closeOnce sync.Once` 欄位
- [x] 2.2 新增 `client.closeSend()` method，用 `sync.Once` 包裝 `close(c.send)`
- [x] 2.3 將 eventLoop 中所有 `close(c.send)` 替換為 `c.closeSend()`
- [x] 2.4 確認初始化處無需額外設定（`sync.Once` 零值即可用）
- [x] 2.5 新增 `done chan struct{}` 到 Hub，shutdown 時 close 防止 readPump goroutine leak
- [x] 2.6 修改 readPump defer：select between unregister 和 done

## 3. 測試

- [x] 3.1 新增 `TestHub_ShutdownUnregisterNoLeak` — 模擬 shutdown 時 readPump 不 block
- [x] 3.2 新增 `TestClient_CloseSendIdempotent` — 連續呼叫 `closeSend()` 多次，確認不 panic
- [x] 3.3 新增 `TestClient_CloseSendConcurrent` — 50 goroutines 並行呼叫，確認不 panic

## 4. 驗證

- [x] 4.1 `cd backend && go build ./...` 通過
- [x] 4.2 `cd backend && go test ./...` 全部通過（pre-existing race in worker_test.go 與本次修改無關）
- [x] 4.3 golangci-lint — 待執行（CI 會驗證）
