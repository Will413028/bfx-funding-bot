## 1. Worker 依賴工廠

- [x] 1.1 Create `lending/fetcher.go` — DataFetcher 實作（Bitfinex REST 拉取 wallet + offers + credits）
- [x] 1.2 Create `lending/adapter.go` — OfferExecutor adapter（execution.OfferExecutor → worker.OfferExecutor 轉換）
- [x] 1.3 Create `lending/factory.go` — DepsFactory struct + BuildWorkerDeps 實作
- [x] 1.4 Add unit tests for fetcher, adapter, factory

## 2. Service 層橋接

- [x] 2.1 Add `WorkerManager` interface to `service/apikey.go`，修改 struct + constructor
- [x] 2.2 Modify `APIKeyService.Create` — verified key + config 存在時 StartWorker（best-effort）
- [x] 2.3 Modify `APIKeyService.Delete` — StopWorker（best-effort）
- [x] 2.4 Add `ConfigReloader` interface to `service/config.go`，修改 struct + constructor
- [x] 2.5 Modify `ConfigService.Save` — ReloadConfig（best-effort）
- [x] 2.6 Update `service/` existing tests for new constructor signatures

## 3. fx 整合 + main.go

- [x] 3.1 Modify `main.go` — 移除 engine.NewEngine + startEngine，新增 lending/marketfeed providers
- [x] 3.2 Add `startLendingEngine` lifecycle hook（marketfeed + lending.Service + snapshot 轉發 + 既有用戶載入）
- [x] 3.3 Wire fx.Annotate + fx.As 讓 lending.Service 滿足 WorkerManager + ConfigReloader

## 4. 清理 + 文件

- [x] 4.1 Delete `internal/engine/` package
- [x] 4.2 Run `go build ./...` + `go test ./...` 確認全部通過
- [x] 4.3 Update `backend_architecture.md` — 反映 WorkerDepsFactory 模式 + 移除 engine 相關描述
