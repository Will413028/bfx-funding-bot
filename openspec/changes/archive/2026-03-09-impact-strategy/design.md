## Context

使用者可能持有大量資金（例如 10 萬 USD），如果全部掛出去，可能佔 ask depth 的 10-20%，這會壓低市場利率並暴露策略。Market Impact 在掛單前評估衝擊，做出調整。

## Goals / Non-Goals

**Goals:**
- 計算自身掛單佔 ask depth 的比例（impact ratio）
- 衝擊超過門檻時加利率溢價補償
- 衝擊嚴重時縮減掛單量
- 純計算，滿足 Apply 介面

**Non-Goals:**
- 不做拆分（D6 的職責）
- 不追蹤歷史衝擊（stateless 計算）

## Decisions

### 1. Impact Ratio 計算

```
existingOfferAmount = sum(activeOffers.Amount)
impactRatio = (existingOfferAmount + newAmount) / askDepth
```

### 2. 三級衝擊門檻

| Impact Ratio | 動作 |
|---|---|
| <= 10% | 正常，不調整 |
| 10-25% | 利率溢價 +3% 補償 |
| > 25% | 利率溢價 +5% 且縮減掛單量至 25% × askDepth - existingAmount |

### 3. Ask Depth 為零時

無市場數據時不做衝擊調整（等同正常）。

## Risks / Trade-offs

- **[門檻值硬編碼]** → 後續可參數化
- **[不考慮 bid side 衝擊]** → 使用者是 offer side，bid 衝擊不相關
