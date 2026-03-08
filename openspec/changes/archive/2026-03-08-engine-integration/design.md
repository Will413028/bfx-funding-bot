## Context

Phase B-E 完成了 lending engine 的所有子模組（marketfeed、signal、orderbook、strategy、execution、worker、quota、service），但 `main.go` 仍使用舊的 `engine.Engine` MVP（每 3 分鐘 tick，直接遍歷 DB 所有 verified key）。新引擎 `lending.Service` 已有完整的 Worker Pool + Quota + Snapshot 廣播機制，但完全未連接到應用啟動流程。

`service/` 層（HTTP CRUD）和 `lending/` 層（引擎）之間缺少橋接 interface：`APIKeyService` 無法啟停 Worker，`ConfigService` 無法熱載入參數。

## Goals / Non-Goals

**Goals:**
- 建立 `WorkerDepsFactory` 的具體實作，將 strategy、execution、bitfinex client 等正確注入每個 Worker
- 在 `service/` 定義 `WorkerManager` 和 `ConfigReloader` consumer-side interface
- 修改 `APIKeyService` 和 `ConfigService` 持有橋接 interface，連動 Worker 生命週期
- 在 `main.go` 透過 fx 完成整合：`lending.Service` + `marketfeed.Service` 取代 `engine.Engine`
- 移除 `engine/` MVP package
- 更新 `backend_architecture.md` 反映實際的 `WorkerDepsFactory` 模式

**Non-Goals:**
- 不重構 strategy 模組（目前 PricingStrategy 作為單一 Strategy 足夠，組合策略留待未來）
- 不修改 marketfeed 的 WS 連線邏輯（已穩定運作）
- 不新增 API endpoint（整合不改變對外介面）
- 不做 multi-instance 分散式部署（保持單體架構）

## Decisions

### 1. WorkerDepsFactory 具體實作放在 `lending/` 頂層

建立 `lending/factory.go`，持有 `bitfinex.Client`、`*crypto.AES`、`repository.APIKeyRepository`、`repository.ExecutionRepository`。`BuildWorkerDeps` 為每個 user 建立：

- **Strategy**: `strategy.NewPricingStrategy()` — 目前所有 user 共用同一策略
- **DataFetcher**: 新建 `lending/fetcher.go`，實作 `worker.DataFetcher`，透過 Bitfinex REST 拉取 wallet + active offers + credits
- **OfferExecutor**: 包裝 `execution.OfferExecutor` 為 `worker.OfferExecutor` adapter（因為兩者的 `ExecutionSummary` 型別不同，需要轉換）

**為什麼不直接在 `worker/` import `execution/`？** 架構規定子 package 之間不互相 import。`worker/` 定義自己的 interface (`OfferExecutor`)，`lending/factory.go` 負責 adapter 轉換。

### 2. Service 層橋接用 consumer-side interface

```go
// service/apikey.go
type WorkerManager interface {
    StartWorker(userID string, cfg domain.StrategyConfig) error
    StopWorker(userID string) error
}
```

```go
// service/config.go
type ConfigReloader interface {
    ReloadConfig(userID string, cfg domain.StrategyConfig) error
}
```

fx 自動注入 `*lending.Service`（隱式實作），無需 import `lending/`。

**APIKeyService 行為變更**：
- `Create()` 成功且 status=verified → 查找 user 的 StrategyConfig → 若存在則 `StartWorker`（best-effort，不影響 API Key 建立結果）
- `Delete()` 成功 → `StopWorker`（best-effort）

**ConfigService 行為變更**：
- `Save()` 成功 → `ReloadConfig`（best-effort，Worker 不存在時忽略錯誤）

### 3. MarketFeed → LendingService 連接

`main.go` 啟動時：

1. `marketfeed.Service.Start()` 開始接收 WS 數據
2. 獨立 goroutine 讀取 `marketfeed.Snapshots()` channel
3. 每收到一個 snapshot → `lending.Service.BroadcastSnapshot(snapshot)`

這個 goroutine 由 `startLendingEngine` lifecycle hook 管理。

### 4. fx 佈線順序

```go
// 新增
fx.Provide(lending.NewDepsFactory),      // → *lending.DepsFactory (實作 WorkerDepsFactory)
fx.Provide(lending.NewService),          // → *lending.Service
fx.Provide(marketfeed.NewService),       // → *marketfeed.Service

// 修改
fx.Provide(service.NewAPIKeyService),    // 新增 WorkerManager 參數
fx.Provide(service.NewConfigService),    // 新增 ConfigReloader 參數

// fx.As 綁定 interface
fx.Provide(
    fx.Annotate(lending.NewService,
        fx.As(new(service.WorkerManager)),
        fx.As(new(service.ConfigReloader)),
    ),
)

// 取代
// fx.Provide(engine.NewEngine)          // 移除
// fx.Invoke(startEngine)                // 移除
fx.Invoke(startLendingEngine)            // 新增
```

**注意**：fx.Annotate + fx.As 讓 `*lending.Service` 同時滿足多個 interface，避免 `service/` import `lending/`。

### 5. Graceful shutdown 順序

fx lifecycle (LIFO):
1. Stop HTTP server（停止新請求）
2. Stop lending.Service（停止所有 Worker + quota refill）
3. Stop marketfeed.Service（關閉 WS 連線）
4. Close DB / Redis connections

### 6. 引擎啟動時載入既有 Worker

`startLendingEngine` 在 OnStart 時：
1. 啟動 `marketfeed.Service`
2. 啟動 `lending.Service`（quota refill + pool）
3. 掃描 DB 中所有 verified API Key + 有效 StrategyConfig 的用戶
4. 對每個用戶呼叫 `lending.Service.StartWorker()`
5. 啟動 snapshot 轉發 goroutine

這樣重啟後所有已設定的用戶自動恢復放貸。

## Risks / Trade-offs

- **[Strategy 未組合]** 目前 13 個策略模組各自獨立，PricingStrategy 只實作了基本的定價邏輯。組合所有模組（floor、period、allocation 等）需要額外的 CompositeStrategy，但這超出整合 scope → 先用 PricingStrategy 確保 pipeline 通暢
- **[Best-effort Worker 啟停]** APIKeyService.Create 若 StartWorker 失敗，API Key 仍成功建立 → 用戶可能不知道 Worker 未啟動。需要 log 記錄 + 未來加通知
- **[啟動順序]** marketfeed 必須在 lending.Service 之前啟動，否則 snapshot channel 可能 miss 最初幾秒 → 可接受，Worker 等到第一個 snapshot 才 tick
- **[engine/ 移除]** 一次性移除舊 MVP，無回退機制 → 新引擎已充分測試，可接受
