## Why

E5 Pool 管理 Worker 集合，但需要具體的 Worker 實作。每個 Worker 是一個獨立 goroutine，負責：接收 MarketSnapshot → 組裝 DecisionContext → 呼叫 Strategy → 執行結果。Worker 需要支援 graceful shutdown、config 熱載入、panic recovery。

## What Changes

- 新增 `lending/worker/worker.go`：LendingWorker 主循環
  - 滿足 E5 定義的 `Worker` interface（Run, Stop, ReloadConfig, UserID）
  - 透過 snapshot channel 接收 MarketSnapshot（event-driven，非 polling）
  - 每收到 snapshot 執行一輪 tick：拉取私有數據 → 組裝 DecisionContext → Strategy.Apply → Executor.ExecuteDecision
  - Config 熱載入：透過 channel 接收新 config，下次 tick 生效
  - Panic recovery：tick panic 不 crash worker，記錄錯誤後繼續
- 新增 `lending/worker/lifecycle.go`：狀態管理
  - WorkerState enum: Starting, Running, Paused, Stopping, Stopped
  - 狀態轉換方法 + 查詢
- Consumer-side interfaces: Strategy, DataFetcher（取代架構文件的 Executor，因為實際執行由 OfferExecutor 處理）
- 新增完整單元測試

## Capabilities

### New Capabilities
- `worker-lifecycle`: 單一 Worker 主循環 + 生命週期管理

### Modified Capabilities

## Impact

- 新增檔案：`lending/worker/worker.go`, `lending/worker/lifecycle.go`, `lending/worker/worker_test.go`, `lending/worker/lifecycle_test.go`
- LendingWorker 滿足 Pool 的 Worker interface
- 定義 consumer-side interfaces（Strategy, DataFetcher, OfferExecutor）
