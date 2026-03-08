## Context

使用者的 funding credits 各有不同到期日。如果大量 credits 集中在同一天到期，重掛時會造成供給衝擊。Stagger Strategy 計算到期分佈的集中度，並調整新掛單的 period 以填補空缺日期。

## Goals / Non-Goals

**Goals:**
- 計算未來各天的到期金額分佈
- 找出到期最少的日期區間，優先讓新掛單在該區間到期
- 用 period 調整實現分散效果
- 純計算，滿足 Apply 介面

**Non-Goals:**
- 不做跨幣種分析（只看當前幣種）
- 不做拆分（只調整 period，拆分由 D6 負責）

## Decisions

### 1. 到期日計算

```
expiryDay = daysUntil(credit.OpenedAt + credit.Period)
```

將 active credits 按 expiryDay 分桶統計金額。

### 2. 最佳 period 選擇

在 config.Period.Min ~ config.Period.Max 範圍內，選擇到期金額最少的 day 作為目標到期日，反推 period。

```
bestPeriod = argmin(day in [Period.Min, Period.Max]) { expiryBucket[day] }
```

如果多個 day 並列最少（都是 0），取中間值以平衡。

### 3. 集中度指標

```
concentration = maxBucketAmount / totalCreditAmount
```

concentration > 50% 時視為過度集中，在 Reason 中標記。無 credits 時 concentration = 0，使用預設 period。

## Risks / Trade-offs

- **[bucket 粒度為天]** → 足夠 funding 場景，不需要更細
- **[不考慮未來新掛單]** → stateless，只看當前 credits
