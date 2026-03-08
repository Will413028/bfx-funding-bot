## Why

每個活躍使用者需要一個獨立的 Worker goroutine 來執行放貸策略。Worker Pool 負責管理所有使用者的 Worker 生命週期：新增、移除、查詢狀態。它是 lending engine orchestrator 和個別 Worker 之間的中介層，確保每個 userID 最多只有一個活躍的 Worker。

## What Changes

- 新增 `lending/worker/pool.go`：Worker Pool 管理
  - `Pool` struct，持有 workers map 和同步鎖
  - `Start(userID, config, deps)` — 啟動新 Worker（如已存在則 error）
  - `Stop(userID)` — 停止並移除指定 Worker
  - `StopAll(ctx)` — 優雅關閉所有 Worker（用於 graceful shutdown）
  - `Reload(userID, config)` — 熱載入新 StrategyConfig
  - `Count()` — 回傳目前活躍 Worker 數量
  - `Has(userID)` — 檢查某 userID 是否有活躍 Worker
  - Worker 結束後自動從 pool 移除（background cleanup）
- 新增完整單元測試

## Capabilities

### New Capabilities
- `worker-pool`: Per-User Worker 池管理

### Modified Capabilities

## Impact

- 新增檔案：`lending/worker/pool.go`, `lending/worker/pool_test.go`
- 定義 `Worker` interface（consumer-side），供 pool 管理 Worker 生命週期
- 後續 E6 實作具體的 Worker struct
