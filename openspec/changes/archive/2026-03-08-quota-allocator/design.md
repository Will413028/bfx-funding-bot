## Context

Bitfinex REST API 有速率限制（約 10-30 req/min 視端點而定）。每個 Worker tick 可能產生多次 API 呼叫（取餘額、取掛單、取 credits、提交 offer × N、撤銷 offer × N）。不限制的話，20+ 使用者同時運作會超過限額。

## Goals / Non-Goals

**Goals:**
- 全局配額池，所有 Worker 共享
- Per-user 配額上限，防止單一使用者壟斷
- Plan-based 差異化：pro/enterprise 比 starter 有更多配額
- Thread-safe（多 Worker goroutine 同時 Acquire）
- 定時 refill 重置 window

**Non-Goals:**
- 不做 Redis-based 分散式配額（單機版，擴展時再改）
- 不做精確的 Bitfinex rate limit 追蹤（用保守的固定配額）
- 不做排隊等待（Acquire 立即回傳 true/false）

## Decisions

### 1. 雙層配額：全局 + per-user

全局 pool 有總容量（如 90 req/min）。每個 user 有 per-user 上限（如 starter=15, pro=30, enterprise=45）。Acquire 需要同時滿足兩個條件：全局剩餘 ≥ n 且 user 剩餘 ≥ n。

### 2. 固定 window 而非 sliding window

每個 refill interval（預設 1 分鐘），重置所有計數器。簡單好實作，且 Bitfinex 的 rate limit 也是固定 window。

### 3. Acquire 是非阻塞的

Worker 呼叫 `Acquire(userID, n)` 時，如果配額不足，立即回傳 false。Worker 可以選擇跳過本輪 tick 或減少操作數量。不做排隊等待，避免 goroutine 阻塞。

### 4. Refill 由外部觸發或 background goroutine

`Allocator` 提供 `Refill()` 方法手動重置。也可啟動 `StartRefill(ctx, interval)` background goroutine 自動定時 refill。測試用手動 Refill。

## Risks / Trade-offs

- **[Under-utilization]** 固定 window 可能在 window 結尾浪費配額 → 可接受，保守策略避免觸發 API 限流
- **[Burst]** Window 開始時所有 Worker 同時衝高 → 全局上限限制 burst 幅度
