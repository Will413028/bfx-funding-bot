## Why

所有子模組（strategy, execution, worker, quota）已就緒，需要一個頂層編排器將它們串聯。`lending.Service` 是放貸引擎的入口點，負責啟動 Worker Pool + Quota Allocator，提供 StartWorker/StopWorker/ReloadConfig 讓 service 層呼叫，並管理 MarketSnapshot 廣播。完成後取代目前的 `engine/` MVP。

## What Changes

- 新增 `lending/service.go`：Engine Orchestrator
  - `Service` struct，持有 workerPool、quota allocator、snapshot broadcaster
  - `NewService(deps)` — 接受依賴並組裝子模組
  - `Start(ctx) error` — 啟動 quota refill + snapshot broadcaster，阻塞直到 ctx 取消
  - `Stop(ctx) error` — 優雅關閉：StopAll workers → 停止 quota refill
  - `StartWorker(userID, config) error` — 建立 Worker 並啟動（設定 quota + 加入 pool）
  - `StopWorker(userID) error` — 停止 Worker（移除 quota + 從 pool 移除）
  - `ReloadConfig(userID, config) error` — 轉發到 pool.Reload
  - `BroadcastSnapshot(snapshot)` — 廣播 MarketSnapshot 到所有 Worker
  - 滿足 `service.WorkerManager` 和 `service.ConfigReloader` interfaces（隱式實作）
- 新增完整單元測試

## Capabilities

### New Capabilities
- `engine-orchestrator`: 引擎頂層編排，整合所有子模組

### Modified Capabilities

## Impact

- 新增檔案：`lending/service.go`, `lending/service_test.go`
- 這是 lending package 的根檔案（`package lending`），import worker/ 和 quota/
- 完成後可取代 `engine/` MVP
