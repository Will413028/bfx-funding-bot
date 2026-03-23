## Context

現有 pipeline 流程：
1. `ComputePeriod()` 算出**單一 period**（regime + rate + volatility + stagger）
2. `ComputeTiers()` 根據餘額和 regime 分出 1-3 個 allocation tier（各有 rate multiplier）
3. 所有 tier 的 offer 使用相同 period

問題：到期集中（同一天大量 credit 到期）導致 renewal 壓力和利率風險。

## Goals / Non-Goals

**Goals:**
- 每次決策產生的 offer 分散在 short/medium/long 三個 period tier
- 續約時根據 RatePercentile 動態升降級 period tier
- 與既有 allocation tier 自然整合（1:1 映射或獨立分配）
- 既有 stagger 邏輯（碰撞避免）在 per-tier 範圍內繼續運作

**Non-Goals:**
- 不做 per-offer 獨立 period 計算（太複雜，每 tier 一個 period 即可）
- 不改 DB schema 或 API contract
- 不做 M7 Temporal Laddering（跨 tick 分散，獨立 change）

## Decisions

### 1. Period Tier 定義方式：靜態比例切割

**決定：** 將 `[Period.Min, Period.Max]` 等分為三段：
- Short: `[Min, Min + range/3]`
- Medium: `[Min + range/3, Min + 2*range/3]`
- Long: `[Min + 2*range/3, Max]`

每個 tier 內使用現有 `ComputePeriod()` 的邏輯（regime/rate/volatility 調整），但 clamp 到 tier 的子範圍。

**理由：** 簡單、可預測、不需要新的 config 欄位。使用者的 `Period.Min/Max` 直接控制整體範圍。

**退化情況：** 若 `Max - Min < 3`，range 太小無法分三層，退化為單一 period（現有行為）。

### 2. Allocation Tier 與 Period Tier 的映射

**決定：** 1:1 映射——core tier 用 short period，moderate 用 medium，aggressive 用 long。

| Allocation Tier | Rate Multiplier | Period Tier |
|-----------------|----------------|-------------|
| Core (保守) | 1.0× | Short（高流動性，快速回收） |
| Moderate | 1.10× | Medium（平衡） |
| Aggressive | 1.12× | Long（鎖定高利率） |

**理由：** 保守部位不鎖長期（降低風險），激進部位鎖長期（鎖定高利率）。這符合傳統債券梯隊的邏輯。

**單 tier 情況：** 若只有 1 個 allocation tier（餘額 < 150 或 crisis），使用 medium period tier。

### 3. RatePercentile 驅動的 tier 偏移

**決定：** 在 `ComputePeriodLadder()` 中，根據 `snapshot.RatePercentile` 偏移各 tier 的 period：
- RatePercentile > 0.5（P75+）：所有 tier 向 Long 偏移（鎖定高利率）
- RatePercentile < -0.5（P25-）：所有 tier 向 Short 偏移（保持靈活）
- 中間：不偏移

偏移量 = `ratePercentile × tierRange × 0.3`（最多偏移 tier range 的 30%）。

### 4. 碰撞避免：在 tier 範圍內做 stagger

**決定：** 現有 `AdjustPeriodForStagger()` 的邏輯不變，但改為在每個 period tier 的子範圍內運作。每個 tier 獨立找該子範圍內的最低密度到期日。

### 5. 新增 PeriodLadder 結構

```go
type PeriodLadder struct {
    Periods []int // 每個 allocation tier 對應的 period（與 tiers 順序一致）
}
```

`CompositeStrategy.Apply()` 在 Stage 3 為每個 tier 分配對應的 period，取代統一的 period。

## Risks / Trade-offs

- **[Risk] 短期 tier 填單率可能較低**（2-3 天 period 不一定有對手方）→ Mitigation：Short tier 仍使用 `Period.Min`（使用者設定的最低值），若使用者設 Min=2 就是接受短期
- **[Risk] 測試覆蓋複雜度增加**（3 tier × 4 regime × percentile 組合）→ Mitigation：focus on edge cases（單 tier 退化、range < 3 退化、crisis override）
