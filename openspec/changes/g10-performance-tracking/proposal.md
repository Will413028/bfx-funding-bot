## Why

放貸引擎目前記錄 ExecutionRecord（action/amount/rate/period/status），但沒有追蹤放貸績效——不知道策略比被動 FRR 跟隨多賺了多少（Alpha），不知道哪個策略模組貢獻最大，也無法根據歷史表現自動微調參數。這些是 strategy_specification.md §9.1-9.3 的核心要求。

為避免 DB schema 擴充（需 Docker），本次使用 Redis 做中間儲存 + 現有 ExecutionRecord 擴展 in-memory 欄位。

## What Changes

- 新增 `lending/tracking/` package — 績效追蹤核心
- Alpha 計算：`Alpha = actual_rate - frr_at_time`，每次掛單成交時記錄
- 策略模組歸因：掛單攜帶 `strategy_tags`，追蹤哪些模組促成了該筆交易
- 自適應參數回饋：基於 7 天歷史 Alpha，每週 ±10% 微調可調參數
- Domain types 擴展：`PerformanceRecord`、`AlphaSummary`、`AdaptiveAdjustment`
- Redis 儲存：per-user 績效記錄 + 參數調整歷史

## Capabilities

### New Capabilities
- `alpha-tracking`: Per-execution alpha 計算 + 歸因標籤 + 儲存
- `adaptive-feedback`: 週期性參數回饋邏輯 — 7 天滾動 alpha 分析 + ±10% 調整

### Modified Capabilities

## Impact

- 新增 `lending/tracking/` — alpha.go, attribution.go, feedback.go
- 新增 `domain/performance.go` — PerformanceRecord, AlphaSummary, AdaptiveAdjustment types
- `lending/worker/worker.go` — tick 後記錄 performance data
- `lending/strategy/` — 各 strategy module 的 Apply 回傳需附帶 strategy tag
- `repository/redis/` — performance record 儲存（7 天 TTL）
