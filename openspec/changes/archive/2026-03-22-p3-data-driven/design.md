## Context

Composite pipeline 已上線（G0），P1 做了 floor/noise/lockup/FRR 防操縱等改進，P2 做了 bestAsk 定價、FRR 趨勢、sigmoid queue、cascade phasing。P3 的改動需要歷史數據累積（percentile、mean reversion、gap cost），因此初期行為會 fallback 到既有邏輯。

## Goals / Non-Goals

**Goals:**
- 5 項 P3 改動全部實作
- RatePercentile 和 mean reversion 在數據不足時 graceful fallback
- Gap cost 追蹤從第一次成交開始累積

**Non-Goals:**
- 不做持久化存儲（percentile 用 in-memory circular buffer，重啟後重新累積）
- 不做 DB schema 變更（G13 才需要）
- 不改 worker.Strategy interface

## Decisions

### D1: G14 Auto-Renew Re-pricing

`execution/credit.go` 的 `ProcessExpiring()` 改為：
1. 對即將到期的 credit，建立 `DecisionContext`（用最新 snapshot + user data）
2. 呼叫 `CompositeStrategy.Apply(ctx)` 取得新的 rate/period
3. 用新參數提交 renew offer
4. 如果 pipeline 失敗，fallback 到原有 auto-renew（已開啟的安全網）

需要 `ProcessExpiring` 接收一個 `Strategy` 參數或在建構時注入。

### D2: G16 Order Book Gap Detection

新增 `orderbook/gap.go`：
```go
type RateGap struct {
    Low    float64 // gap 開始（低利率端）
    High   float64 // gap 結束（高利率端）
    Width  float64 // High - Low
}

func DetectGaps(entries []BookEntry, minGapWidth float64) []RateGap
```

掃描 ask side entries（按 rate 排序），找相鄰 entries 間的 rate 空隙。`minGapWidth` 預設為 spread × 3（顯著空隙）。

在 `ComputeBaseRate` 中，如果目標 rate 落在某個 gap 內，直接使用 gap.Low（gap 的最低端 = 最容易成交的位置）。

### D3: S4 RatePercentile Signal

新增 `signal/percentile.go`：
```go
type RatePercentile struct {
    buffer []float64 // circular buffer of historical rates
    head   int
    count  int
    size   int       // max buffer size (7d × snapshots per day)
}

func (r *RatePercentile) Compute(data *RawMarketData) SignalValue {
    // Add current rate to buffer
    // Compute percentile rank of current rate
    // Output: Value = (percentile - 0.5) × 2, normalized to [-1, +1]
    //   P75 → +0.5, P25 → -0.5, P50 → 0
}
```

Buffer size: 7 天 × 每天 28800 snapshots（3 秒間隔）= 201600。為節省記憶體，每 10 個 snapshot 取樣一次 → buffer size = 20160。

在 `MarketSnapshot` 新增 `RatePercentile float64` 欄位。Composite pipeline 用於影響 deploymentRatio 或 period 決策。

### D4: S10 Mean-Reversion P(higher)

新增 `strategy/meanreversion.go`：
```go
func PHigherRate(currentRate, ema7d, rateVolatility float64, waitHours float64) float64 {
    if rateVolatility <= 0 || waitHours <= 0 {
        return 0.5 // no info → 50/50
    }
    // Φ((μ - current) / (σ × √t))
    z := (ema7d - currentRate) / (rateVolatility * math.Sqrt(waitHours))
    return normalCDF(z)
}

func normalCDF(x float64) float64 {
    return 0.5 * (1 + math.Erf(x / math.Sqrt2))
}
```

在 composite pipeline 的 floor 階段使用：如果 `PHigherRate > 0.7`（70% 機率利率會上升），可以容忍更高的 floor（等待更好利率）。如果 `< 0.3`（利率大概率不會更高），立即放貸。

### D5: M2 Gap Cost Tracking + Pre-scheduling

**Gap cost tracking**：`worker.go` 新增 `lastCreditExpiry time.Time` 和 `gapMinutesEMA float64`。每次成功提交 offer 時計算 gap = `now - lastCreditExpiry`，更新 EMA。

**Pre-scheduling**：composite pipeline 在有即將到期的 credit 時（< 1 tick 週期），提前計算下一筆 offer 並準備好（但不提交），到期瞬間立即提交。

**Period 影響**：`gapCost = avgGapMinutes / (period × 1440)`。Period 選擇時加入 gap cost penalty，短期 period 的 gap cost 更高。

`DecisionContext` 新增 `AvgGapMinutes float64`。

## Risks / Trade-offs

- **[S4 記憶體使用]** → 20160 float64 = ~160KB per symbol，可接受
- **[S10 rateVolatility 來源]** → 使用 `RegimeParams.Volatility`（已有），不需新增
- **[G14 需要 Strategy 注入到 credit executor]** → 改變 `CreditExecutor` 的建構函數，需要更新 DI
- **[M2 pre-scheduling 需要 credit expiry 預測]** → 從 `ActiveCredits` 的 `OpenedAt + Period` 計算
