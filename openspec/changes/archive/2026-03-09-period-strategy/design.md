## Context

Bitfinex funding 天數範圍 2-120 天。短天數（2 天）保持靈活性但需要頻繁重新掛單；長天數（30-120 天）鎖定利率但喪失調整機會。Period Strategy 根據市場狀態在這兩者之間取得平衡。

D1 Pricing 和 D2 Floor 已完成利率計算。Period 是策略 pipeline 的第三個模組，獨立計算天數決策。

## Goals / Non-Goals

**Goals:**
- 根據 regime、利率水準、波動率動態計算建議天數
- 純計算、滿足 `Apply(*DecisionContext) *DecisionResult` 介面
- 結果 clamp 到使用者設定範圍

**Non-Goals:**
- 不考慮到期時間分散（D10 Stagger 的職責）
- 不修改利率（D1 的職責）

## Decisions

### 1. Regime 驅動基礎天數

每個 regime 對應一個基礎天數比例，映射到 config 的 Period 範圍：

- **Contango**：75% 的範圍（偏長，鎖定上行趨勢）
- **Neutral**：50% 的範圍（中間值）
- **Backwardation**：25% 的範圍（偏短，保持靈活）
- **Crisis**：0%（直接用 Period.Min，最短天數）

```
basePeriod = Period.Min + ratio × (Period.Max - Period.Min)
```

### 2. 利率縮放

當實際利率相對 FRR 較高時，值得鎖定更長天數：

```
rateRatio = offeredRate / FRR   (capped at 2.0)
scaleFactor = 1.0 + (rateRatio - 1.0) × 0.3   (±30% 調整)
adjustedPeriod = basePeriod × scaleFactor
```

如果 FRR 為零或利率為零，跳過此步驟。

### 3. 波動率縮短

高波動時縮短天數保持靈活性：

```
if volatility > 0.10:
    volDiscount = 1.0 - min(volatility - 0.10, 0.20) × 2.0  // 最多打 6 折
    adjustedPeriod = adjustedPeriod × volDiscount
```

波動率來自 `RegimeParams.Volatility`（EMA 平滑後的 |DailyChangePerc|）。

### 4. 四捨五入為整數

天數必須是整數，使用 `math.Round` 後 clamp 到 config 範圍。

## Risks / Trade-offs

- **[Regime 比例硬編碼]** → 後續可參數化。目前 75/50/25/0 的階梯足夠合理
- **[利率縮放 ±30% 可能不夠]** → 保守起步，觀察效果後調整
- **[波動率閾值 0.10 的選擇]** → 基於經驗值，10% 日波動已算相當高
