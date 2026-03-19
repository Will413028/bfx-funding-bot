## Context

現有 ExecutionRecord 記錄 action/amount/rate/period/status，但缺少 FRR baseline、MDC score、regime、strategy tags 等績效分析所需資訊。§9.1 要求 per-execution alpha tracking，§9.2 要求 40 模組歸因，§9.3 要求每週自適應回饋。

完整實作需要 DB schema 擴充（新增欄位到 executions 表），但目前沒有 Docker 環境跑 Atlas migration。務實做法：新增 `tracking/` package 做核心邏輯，用 Redis 做中間儲存（7 天 TTL），未來補 DB persistence。

## Goals / Non-Goals

**Goals:**
- `PerformanceRecord` type 捕捉 §9.1 核心欄位（alpha, frr, mdc, regime, strategy_tags）
- `AlphaCalculator` 計算 per-execution alpha + 滾動摘要
- `AttributionTracker` 按 strategy tag 分組歸因
- `AdaptiveFeedback` 基於 7 天 alpha 產生 ±10% 參數調整建議
- Redis 儲存 per-user performance records（7 天 TTL）
- Worker tick 後記錄 performance

**Non-Goals:**
- DB schema migration（未來做）
- Dashboard API endpoints（未來做）
- 完整 40 模組歸因（先做框架，模組自行掛載 tag）
- 自動套用參數調整到 StrategyConfig（先產生建議，手動確認）

## Decisions

### 1. 新增 `lending/tracking/` package，不改現有 execution

**選擇**：平行的績效追蹤系統，不修改 ExecutionRecord DB schema。

**理由**：避免 DB migration 阻塞。Performance data 存 Redis（7d TTL），未來有 Docker 時再加 DB 欄位。

### 2. PerformanceRecord 用 Redis JSON 儲存，key = `perf:{userID}:{timestamp}`

**選擇**：每筆 performance record 獨立存 Redis，TTL 7 天。查詢時 scan by prefix。

**替代**：用 Redis List append。但 per-key 更容易設 TTL，且 7 天內的 records 數量可控（每天最多幾百筆）。

### 3. AdaptiveFeedback 產生建議，不自動套用

**選擇**：feedback.Analyze() 回傳 `[]AdaptiveAdjustment`，由外部決定是否套用。

**理由**：自動套用需要修改 StrategyConfig persistence，超出本次範圍。先做分析邏輯，套用機制未來加。

## Risks / Trade-offs

- **[Risk] Redis 重啟丟失 7 天追蹤資料** → 可接受，重新累積即可
- **[Trade-off] 不改 DB → 無法做長期趨勢分析** → 未來補 migration
- **[Trade-off] 建議不自動套用 → 需要人工審核** → 安全優先
