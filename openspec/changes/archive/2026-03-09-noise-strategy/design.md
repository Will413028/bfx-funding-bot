## Context

自動化放貸策略的掛單行為若過於規律（固定利率、固定金額），容易被其他參與者辨識並利用。加入 noise 讓掛單看起來像人工操作。

## Goals / Non-Goals

**Goals:**
- 利率加入 ±1% 隨機擾動
- 金額加入 ±2% 隨機擾動
- 避開心理價位（0.0005 的倍數），偏移 ±0.00001
- 結果仍受 config bounds 約束
- 純計算，滿足 Apply 介面

**Non-Goals:**
- 不做加密安全隨機（math/rand 足夠）
- 不追蹤歷史噪聲模式

## Decisions

### 1. 利率擾動

```
rateNoise = rate × uniform(-0.01, +0.01)
adjustedRate = rate + rateNoise
```

### 2. 金額擾動

```
amountNoise = amount × uniform(-0.02, +0.02)
adjustedAmount = amount + amountNoise
```

### 3. 心理價位避讓

檢查 rate 是否接近 0.0005 的倍數（容差 0.00001）。若是，向上偏移 0.00002 避開整數位。

### 4. 可測試性

接受 `rand` 函數注入，測試時使用固定種子確保可重複。

## Risks / Trade-offs

- **[噪聲幅度硬編碼]** → 後續可參數化
- **[math/rand 不加密安全]** → 此場景不需要加密安全
