## Context

D1 Pricing 算利率，D2 Floor 設地板，D3 Period 算天數。但目前利率和天數是獨立計算的。實際上，長天數鎖定意味著放棄未來可能出現的更好利率。Lockup Cost 量化這個機會成本，產出一個「鎖倉溢價」——如果目標利率無法補償鎖倉成本，應該要求更高利率或縮短天數。

## Goals / Non-Goals

**Goals:**
- 量化鎖倉機會成本：天數 × 波動率 → 所需最低利率溢價
- 輸出鎖倉調整後的利率（加上溢價）和天數建議
- 純計算，滿足 `Apply(*DecisionContext) *DecisionResult`

**Non-Goals:**
- 不取代 Pricing 或 Period 的計算，而是提供修正建議
- 不考慮具體的替代投資方案（那是 Floor 的職責）

## Decisions

### 1. 鎖倉成本公式

```
lockupCost = baseCostPerDay × period × (1 + volMultiplier × volatility)
```

- `baseCostPerDay`：每天的基礎機會成本（default 0.5 bps = 0.000005）
- `period`：出借天數
- `volMultiplier`：波動率放大係數（default 5.0）
- `volatility`：來自 RegimeParams.Volatility

例：30 天 + 10% 波動率 → `0.000005 × 30 × (1 + 5.0 × 0.10) = 0.000225`

### 2. 利率溢價應用

策略輸出的利率 = 輸入利率 + lockupCost。

如果輸入利率為零（沒有前置 pricing），使用 FRR 作為基準。

### 3. 天數建議

如果 lockupCost 超過利率的一定比例（default 20%），建議縮短天數至 lockupCost 恰好在門檻內的值。

```
if lockupCost / rate > maxCostRatio:
    suggestedPeriod = maxCostRatio × rate / (baseCostPerDay × (1 + volMul × vol))
```

### 4. Regime 調整

Crisis 時鎖倉成本加倍（×2.0），因為未來不確定性最大。
Backwardation 時 ×1.5。

## Risks / Trade-offs

- **[baseCostPerDay 0.5 bps 的合理性]** → 經驗值，後續可觀察實際成交數據調整
- **[maxCostRatio 20% 的門檻]** → 保守值，避免過度懲罰長天數
