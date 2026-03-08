## Why

Phase B-E 實作了完整的放貸引擎（市場分析、策略決策、執行層、Worker Pool、Quota、Service 編排），但這些模組全部處於離線狀態。`main.go` 仍使用舊的 `engine.Engine` MVP（3 分鐘 ticker loop），新引擎 `lending.Service` 未接入 fx 啟動流程。`service/` 層缺少 `WorkerManager` 和 `ConfigReloader` 橋接 interface，導致 API Key 新增/刪除時無法啟停 Worker，策略參數修改時無法熱載入。這是讓引擎真正上線的最後一哩路。

## What Changes

- 建立 `WorkerDepsFactory` 的具體實作，組裝 execution、strategy 等依賴注入到每個 Worker
- 在 `service/` 層定義 `WorkerManager` 和 `ConfigReloader` consumer-side interface
- 修改 `APIKeyService` 持有 `WorkerManager`，在 API Key 建立/刪除時啟停 Worker
- 修改 `ConfigService` 持有 `ConfigReloader`，在參數更新時通知 Worker 熱載入
- 在 `main.go` 中用 `lending.NewService` + fx 取代 `engine.NewEngine`
- 連接 `marketfeed.Service` 的 snapshot 輸出到 `lending.Service.BroadcastSnapshot()`
- 移除舊的 `engine/` MVP package
- 更新 `backend_architecture.md` 反映 `WorkerDepsFactory` 模式

## Capabilities

### New Capabilities

- `engine-wiring`: 引擎整合佈線 — WorkerDepsFactory 實作、fx 接入、service 層橋接、marketfeed 連接

### Modified Capabilities

(無既有 spec 需要修改)

## Impact

- `cmd/server/main.go` — fx provider 替換（engine → lending）
- `service/apikey.go` — 新增 WorkerManager 依賴
- `service/config.go` — 新增 ConfigReloader 依賴
- `lending/service.go` — 可能微調 constructor 簽名
- `engine/` — 整個 package 移除
- `backend_architecture.md` — 更新設計文件
