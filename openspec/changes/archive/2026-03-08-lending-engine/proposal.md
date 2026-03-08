## Why

後端 CRUD 層和 Bitfinex REST API client 都已完成，但系統還沒有核心的自動放貸邏輯。`strategy_specification.md` 定義了完整的策略系統（複合信號、市場體制識別、深度感知拆分等），但需要分階段實作。本 change 實作 **MVP 放貸引擎**，建立引擎框架和基礎策略循環，為後續進階策略模組打基礎。

## What Changes

- 新增 `internal/engine/` package — 放貸引擎核心框架
- 新增 `engine.Engine` struct — 背景 worker，定期為每個活躍用戶執行放貸策略
- 新增 `engine.Strategy` — MVP 策略執行器（基於用戶 StrategyConfig 的固定區間策略）
- 實作基礎放貸循環：查餘額 → 檢查 active offers → 決定是否掛單 → 提交 offer
- 實作僵屍單 TTL 撤銷（spec §7.1）：超時未成交的 offer 自動撤銷重掛
- 實作利息即時再投資（spec §7.1）：可用餘額含新結算利息時納入掛單資金池
- 透過 fx lifecycle 管理引擎生命週期（graceful start/stop）

**MVP 範圍（對應 strategy_specification.md）：**
- §6.1 定時心跳（ticker-based，活躍期 3 分鐘）
- §7.1 基礎複利循環（查餘額 → 掛單 → 僵屍單 TTL → 利息再投資）
- §8.2 基礎安全（餘額 < 50 USD 待機）

**非 MVP（留到後續 change）：**
- §2 複合信號決策框架（MDC）— 需要 WebSocket + 市場數據
- §3 掛單簿分析 — 需要 WebSocket
- §4 進階執行策略（分層佈署、隱藏單、深度感知拆分等）
- §5 動態天數決策 — MVP 用 config 設定的固定天數
- §6.1 事件心跳 — 需要 WebSocket
- §9 績效追蹤 — 留到 funding-history change

## Capabilities

### New Capabilities
- `lending-engine-core`: 放貸引擎核心框架（背景循環、用戶遍歷、引擎生命週期）
- `lending-strategy`: MVP 放貸策略（固定區間策略 + 僵屍單 TTL + 利息再投資）

### Modified Capabilities
（無）

## Impact

- **新增 package**: `internal/engine/`（engine.go、strategy.go）
- **修改 main.go**: fx.Provide engine, fx.Invoke 啟動引擎
- **依賴**: `bitfinex.Client`、`APIKeyRepository`、`ConfigRepository`、`crypto.AES`、`zap.Logger`
- 不修改現有 API endpoints — 引擎是背景 worker
- 不新增外部依賴 — 使用 Go stdlib `time.Ticker` + goroutine
