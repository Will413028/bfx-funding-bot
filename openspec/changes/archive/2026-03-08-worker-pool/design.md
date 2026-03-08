## Context

架構文件定義了 `worker/pool.go` 管理所有使用者的 Worker，`lending/service.go` 透過 Pool 啟停 Worker。每個 Worker 是一個獨立 goroutine，接收 MarketSnapshot 並執行策略。Pool 需要 thread-safe（多個 goroutine 可能同時操作 pool）。

## Goals / Non-Goals

**Goals:**
- Thread-safe 的 Worker 管理（sync.RWMutex）
- 每個 userID 最多一個 Worker（唯一性保證）
- Start / Stop / StopAll / Reload / Count / Has 基本 CRUD 操作
- Worker 結束後自動清理（不留殭屍 entry）
- StopAll 支援 context timeout（graceful shutdown）

**Non-Goals:**
- 不實作 Worker 本身的主循環邏輯（E6 負責）
- 不處理 MarketSnapshot 分發（E8 Orchestrator 負責）
- 不實作 API quota 分配（E7 負責）

## Decisions

### 1. Pool 持有 Worker interface，不持有具體實作

Pool 定義 `Worker` interface：`Run(ctx) error`、`Stop()`、`ReloadConfig(config)`、`UserID() string`。這讓 Pool 不依賴 Worker 的具體實作，可以用 mock 測試。E6 的 Worker struct 只需滿足此 interface。

### 2. 每個 Worker 用獨立 context 管理

`Start()` 為每個 Worker 建立 child context（從 pool 的 root context 派生）。`Stop()` 取消該 context。`StopAll()` 取消 root context，所有 Worker 同時收到取消訊號。

### 3. WorkerFactory 模式

Pool 不自己建立 Worker，而是接受 `WorkerFactory func(userID, config, ctx) Worker`。這讓測試可以注入 mock Worker，production 由 service.go 注入真實的 Worker constructor。

### 4. Background cleanup goroutine

Worker.Run() 結束後（正常或異常），cleanup goroutine 自動從 map 移除 entry。Pool 使用 `sync.RWMutex` 保護 map 存取。

## Risks / Trade-offs

- **[Race on Stop + cleanup]** Stop() 和 cleanup goroutine 可能同時操作同一 entry → 用 mutex + 檢查 entry 存在性避免 double-remove
- **[StopAll timeout]** 如果某個 Worker 卡住不結束，StopAll 可能超時 → 由 caller 決定 context timeout，Pool 只負責通知取消
