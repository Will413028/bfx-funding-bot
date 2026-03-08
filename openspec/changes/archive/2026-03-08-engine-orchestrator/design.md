## Context

架構文件定義 `lending.Service` 為引擎頂層，持有 workerPool + quota allocator。它是 service 層的 `WorkerManager` 和 `ConfigReloader` interface 的隱式實作。目前 `engine/` 是 MVP ticker loop，完成 lending.Service 後可取代。

MarketSnapshot 廣播：marketfeed 產生 snapshot 後，需要分發給所有 Worker。E8 使用 snapshot channel fan-out 實現。

## Goals / Non-Goals

**Goals:**
- Service struct 整合 worker.Pool + quota.Allocator
- Start/Stop 管理完整生命週期（quota refill goroutine + pool shutdown）
- StartWorker/StopWorker 整合 quota 分配 + pool 管理
- ReloadConfig 轉發到 pool
- BroadcastSnapshot 廣播到所有 Worker 的 snapshot channel
- WorkerDeps factory：為每個新 Worker 注入正確的依賴（Strategy, DataFetcher, OfferExecutor, snapshotCh）
- 滿足 service.WorkerManager + service.ConfigReloader interfaces

**Non-Goals:**
- 不實作 marketfeed 整合（marketfeed 已獨立運作，snapshot 由外部傳入）
- 不修改 service/ 層（已有 WorkerManager/ConfigReloader interface）
- 不刪除 engine/ MVP（保留直到確認 lending/ 全部運作）

## Decisions

### 1. Service 持有 snapshot channels map

每個 Worker 需要自己的 snapshot channel（buffered, cap=1）。Service 維護 `map[string]chan *domain.MarketSnapshot`，在 StartWorker 時建立，StopWorker 時關閉。BroadcastSnapshot 遍歷 map 非阻塞寫入（drop stale）。

### 2. Plan-based quota 映射

StartWorker 接收 config，Service 根據使用者 plan 決定 per-user quota：
- starter: 15 req/min
- pro: 30 req/min
- enterprise: 45 req/min
- free: 0（不啟動 worker）

這個映射由 `planQuota` 函式完成。

### 3. WorkerFactory 注入

Service 接受 `worker.Deps` 的 factory dependencies（Strategy, DataFetcher, OfferExecutor），在 StartWorker 時為每個 Worker 組裝完整的 `worker.Deps`。這避免 Service 直接 import strategy/ 或 execution/。

### 4. Graceful shutdown 順序

Stop: (1) StopAll workers, (2) cancel quota refill。這確保所有進行中的 API 呼叫完成後才停止配額管理。

## Risks / Trade-offs

- **[Snapshot drop]** 如果 Worker tick 太慢，BroadcastSnapshot 會 drop stale snapshot → 可接受，Worker 總是用最新的
- **[Channel leak]** StartWorker 建立 channel，StopWorker 需確保關閉 → Stop 時清理 map
