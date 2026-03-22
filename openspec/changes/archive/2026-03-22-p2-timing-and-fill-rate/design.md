## Context

Composite pipeline 已上線（G0），所有 13 個策略模組通過 exported helper 函數串連。P1 改進了 floor、noise、lockup、FRR 防操縱等。P2 的 7 項改動涉及新信號源和定價邏輯重構，比 P1 更深入。

## Goals / Non-Goals

**Goals:**
- 7 項 P2 改動全部實作，提升擇時能力和 fill rate
- 新信號源（FRR trend、rate spike）整合到 MarketSnapshot
- 定價模型從 FRR-based 轉為 bestAsk-relative

**Non-Goals:**
- 不做 DB schema 變更
- 不改 worker.Strategy interface
- 不做歷史數據回填（信號啟動後開始累積）

## Decisions

### D1: G12 FRR Trend — 新增 signal/frrtrend.go

新增信號源，追蹤 FRR 的短期和長期 EMA：
```go
type FRRTrend struct {
    shortEMA float64 // EMA(30min) ≈ 10 snapshots at 3sec interval
    longEMA  float64 // EMA(4hr) ≈ 4800 snapshots
    count    int
}

func (f *FRRTrend) Compute(frr float64) domain.SignalValue {
    // Update EMAs
    // Trend = (shortEMA - longEMA) / longEMA
    // Positive → rates rising → pricing 更積極
    // Negative → rates falling → 搶先成交
}
```

**整合**：在 `MarketSnapshot` 新增 `FRRTrend float64` 欄位（-1 到 +1），composite pipeline 在 Stage 1 使用。上升趨勢時 rate 乘以 `1.0 + trend × 0.1`（最多 +10%）。

### D2: G15 Smart Wall Positioning — 修改 ComputeBaseRate

目前：靠近 wall → `rate × 0.99`（盲目降 1%）。
改為：靠近 wall → `rate = wallRate - minTickSize`（搶先排在 wall 前面）。

```go
func smartWallPosition(rate float64, walls []domain.WallPosition) float64 {
    for _, w := range walls {
        if w.Side != "offer" { continue }
        distance := math.Abs(w.Rate - rate) / rate
        if distance < wallProximityPct {
            // Price just below the wall to get filled first
            return w.Rate - minTickSize
        }
    }
    return rate
}
```
`minTickSize = 0.00000001`（Bitfinex 最小 tick）。

### D3: GT6 Queue Discount Sigmoid — 修改 ComputeQueueDiscount

從兩段式跳躍改為 smoothstep sigmoid：
```go
func ComputeQueueDiscount(rate float64, ob domain.OrderBookSummary) float64 {
    // ... queue ratio calculation unchanged ...

    // Sigmoid discount: smooth transition from 1.0 to 0.95
    // smoothstep maps [0.1, 0.6] → [0.0, 1.0]
    t := smoothstep(queueRatio, 0.1, 0.6)
    return 1.0 - maxQueueDiscount * t  // maxQueueDiscount = 0.05
}

func smoothstep(x, edge0, edge1 float64) float64 {
    t := clamp((x - edge0) / (edge1 - edge0), 0, 1)
    return t * t * (3 - 2*t)
}
```

### D4: S1 BestAsk-Relative Pricing — 修改 ComputeBaseRate

定價邏輯從「FRR × MDC premium × regime」改為「bestAsk - tickOffset(MDC, regime)」：
```go
func ComputeBaseRate(snap *domain.MarketSnapshot, cfg *domain.StrategyConfig) float64 {
    bestAsk := snap.OrderBook.BestAsk
    if bestAsk <= 0 {
        // Fallback to FRR-based pricing
        return frrBasedRate(snap, cfg)
    }

    // Tick offset based on MDC and regime
    offset := computeTickOffset(snap.MDC.Score, snap.Regime)
    rate := bestAsk - offset

    // Apply depth pressure and FRR trend
    rate = applyDepthPressure(rate, snap.OrderBook)
    rate = applyFRRTrend(rate, snap.FRRTrend)

    return rate
}

func computeTickOffset(mdcScore float64, regime domain.RegimeType) float64 {
    // Base offset: 2 ticks
    baseOffset := 2.0 * minTickSize

    // MDC adjustment: bullish → smaller offset (closer to bestAsk)
    mdcFactor := 1.0 - mdcScore  // MDC +1 → factor 0, MDC -1 → factor 2

    // Regime adjustment
    regimeFactor := 1.0
    switch regime {
    case domain.RegimeContango:
        regimeFactor = 0.5  // aggressive: halve offset
    case domain.RegimeBackwardation:
        regimeFactor = 2.0  // conservative: double offset
    case domain.RegimeCrisis:
        regimeFactor = 0.0  // max aggressive: zero offset (match bestAsk)
    }

    return baseOffset * mdcFactor * regimeFactor
}
```

**FRR fallback**：如果 bestAsk 不可用（order book empty），回退到原有的 FRR-based 定價。

### D5: S5 Cascade Phase Response — 修改 liquidation + composite

在 `LiquidationCascade` 新增 phase 追蹤：
```go
type CascadePhase string
const (
    CascadePhaseNone  CascadePhase = "none"
    CascadePhaseEarly CascadePhase = "early"   // 0-30min
    CascadePhaseMid   CascadePhase = "mid"     // 30min-2hr
    CascadePhaseLate  CascadePhase = "late"    // 2hr+
)
```

在 `MarketSnapshot` 新增 `CascadePhase` 欄位。Composite pipeline 根據 phase 調整 period：
- Early → `period = cfg.Period.Min`（2d 捕暴利）
- Mid → `period = clamp(14, cfg.Period.Min, cfg.Period.Max)`
- Late → 不產生新 offer（return early）

### D6: S6 Dynamic Weekend Premium — 修改 weekend.go

`ComputeWeekendMultiplier` 改為接收歷史 ratio 而非硬編碼：
```go
func ComputeWeekendMultiplier(t time.Time, historicalRatio float64) float64 {
    if !isWeekend(t) {
        return 1.0
    }
    if historicalRatio > 0 {
        return historicalRatio  // e.g., 1.3 means weekend rates are 30% higher
    }
    // Fallback to fixed premiums if no historical data
    return fixedWeekendPremium(t)
}
```

歷史 ratio 由 `marketfeed` 或 `tracking` 模組計算並存入 `MarketSnapshot.WeekendRatio`。初期用 fallback 值，待 G13 (fill rate learning) 上線後用實際數據。

### D7: M5 Rate Spike Detector — 新增 signal/ratespike.go

獨立於清算瀑布的利率飆升偵測：
```go
type RateSpikeDetector struct {
    emaAlpha float64   // EMA smoothing factor for 1hr window
    ema1h    float64
    count    int
}

func (r *RateSpikeDetector) Compute(data *domain.RawMarketData) domain.SignalValue {
    currentRate := data.Ticker.FRR
    r.updateEMA(currentRate)

    if r.count < minSamples { return zero }

    ratio := currentRate / r.ema1h
    if ratio > spikeThreshold {  // 2.0 = rate doubled vs 1hr average
        return domain.SignalValue{
            Type: domain.SignalRateSpike,
            Value: min((ratio - 1.0), 1.0),  // normalized 0-1
            Confidence: min(float64(r.count) / float64(minSamples), 1.0),
        }
    }
    return zero
}
```

在 composite 中：spike detected → override period to `cfg.Period.Min`（搶短期高利率）。

## Risks / Trade-offs

- **[S1 bestAsk pricing 是最大改動]** → 完全改變定價邏輯。保留 FRR fallback，並通過 A/B 測試驗證
- **[S6 缺少歷史數據]** → 初期用 fallback 固定值，等累積 4 週數據後自動切換
- **[M5 spike 偵測的 false positive]** → 門檻設高（2×），避免正常波動觸發
- **[MarketSnapshot 新增欄位]** → 3 個新欄位（FRRTrend, CascadePhase, WeekendRatio），向後相容（零值 = 不啟用）
