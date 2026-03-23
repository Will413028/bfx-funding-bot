## Context

Worker 的 tick 在每次 MarketSnapshot 到達時觸發（活躍期每 3 分鐘、閒置期每 15 分鐘）。每次 tick：
1. Fetch user data（ActiveOffers, ActiveCredits, Available）
2. Build DecisionContext（Available = 實際可用餘額）
3. Strategy.Apply() → 產出所有 offers
4. Executor 一次性執行所有 offers

M7 要在步驟 2 和 3 之間插入限縮邏輯。

## Goals / Non-Goals

**Goals:**
- 將可用資金分三批部署，每批使用當下最新的 snapshot 計算利率
- 實作在 Worker 層級，策略不需改動
- 保持簡單：不引入新的 timer 或 goroutine

**Non-Goals:**
- 不做可配置的批次數量（固定 3 批）
- 不做跨 tick 的 offer 快取（每批都重新走完整策略）
- 不改 DecisionContext 或 DecisionResult 的結構

## Decisions

### 1. Worker 層級限縮，策略不改

**決定：** 在 `tick()` 中，將 `decisionCtx.Available` 乘以 `trancheRatio[trancheIndex]`，讓策略以為只有 1/3 的資金可用。

```go
trancheRatios = [3]float64{1.0/3.0, 1.0/2.0, 1.0}
```

**理由：**
- T+0：trancheIndex=0，Available × 1/3 → 部署 1/3
- T+1：trancheIndex=1，Bitfinex 已扣除 T+0 的部署量，剩餘 2/3，× 1/2 = 部署 1/3 of original
- T+2：trancheIndex=2，剩餘 1/3，× 1.0 = 部署 1/3 of original

每批都重新走策略（用最新 snapshot），自然得到 DCA 效果。

### 2. Reset 條件

**決定：**
- trancheIndex 到 3 → reset 為 0
- ReloadConfig → reset 為 0（使用者改了策略）
- Available < minBalance × 2 → skip laddering，一次部署全部（太少不值得分批）

### 3. 不引入新 timer

**決定：** 依賴現有的 snapshot channel 驅動 tick。心跳間隔已是 3 分鐘，天然符合 spec 的 T+0 / T+3min / T+6min 設計。

## Risks / Trade-offs

- **[Risk] 若心跳間隔不穩定（事件驅動的即時 tick），可能在短時間內部署 2/3** → Mitigation：spec 本意是分散時間風險，即使間隔不精確也比一次性部署好
- **[Risk] 閒置期心跳 15 分鐘，3 批要 45 分鐘** → Mitigation：閒置期代表沒有顯著市場活動，慢一點部署反而更安全
