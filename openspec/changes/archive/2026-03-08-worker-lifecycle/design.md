## Context

架構文件定義 Worker 為獨立 goroutine，接收 MarketSnapshot 後執行放貸策略。E5 Pool 期望 Worker 滿足 `Run(ctx) error`, `Stop()`, `ReloadConfig(config)`, `UserID()` interface。

Worker 主循環是 event-driven：等待 MarketSnapshot（從共享 channel 接收），每次收到就執行一輪 tick。非 timer-based polling。

## Goals / Non-Goals

**Goals:**
- 實作 LendingWorker 滿足 E5 Worker interface
- Event-driven tick：透過 snapshot channel 觸發
- 每輪 tick：DataFetcher 拉私有數據 → 組裝 DecisionContext → Strategy.Apply → OfferExecutor.ExecuteDecision
- Config 熱載入：channel-based，下次 tick 生效
- Panic recovery：單輪 tick panic 不 crash worker
- WorkerState 狀態追蹤

**Non-Goals:**
- 不實作 Strategy / DataFetcher / OfferExecutor 具體邏輯（已在 D1-D13, E1-E4 完成）
- 不處理 MarketSnapshot 的廣播機制（E8 Orchestrator 負責）
- 不做 API quota 管理（E7 負責）

## Decisions

### 1. Consumer-side interfaces

Worker 定義自己需要的 interfaces，不 import sibling packages：
- `Strategy` — `Apply(*domain.DecisionContext) *domain.DecisionResult`
- `DataFetcher` — `FetchUserData(ctx, userID) (*UserData, error)` — 拉取 wallet、active offers、active credits
- `OfferExecutor` — `ExecuteDecision(ctx, userID, *domain.DecisionResult) (*ExecutionSummary, error)`

### 2. Snapshot channel 而非 timer

Worker 的 `Run` 方法 select 在三個 channel 上：
1. `snapshotCh <-chan *domain.MarketSnapshot` — 新的市場快照
2. `configCh <-chan domain.StrategyConfig` — 熱載入
3. `ctx.Done()` — 關閉訊號

這讓 tick 頻率由 marketfeed 決定，Worker 不需要自己管理 timer。

### 3. ReloadConfig 用 buffered channel

`configCh` 容量為 1。`ReloadConfig` 非阻塞寫入：先 drain 再 send。避免 caller 被 block。

### 4. Panic recovery 在 tick 層級

每輪 tick 用 `defer recover()` 保護。panic 被捕獲後記錄錯誤，Worker 繼續等待下一個 snapshot。

### 5. WorkerState 用 atomic

狀態用 `atomic.Int32` 存儲，免 mutex。狀態轉換：Starting → Running → Stopping → Stopped。Paused 用 flag 控制（跳過 tick，不退出 loop）。

## Risks / Trade-offs

- **[Stale snapshot]** 如果 tick 時間 > snapshot 間隔，會漏掉中間的 snapshot → 可接受，Worker 總是用最新的 snapshot
- **[Config race]** ReloadConfig 和 tick 可能同時存取 config → 用 channel 序列化，tick 在 select 中讀取 config
