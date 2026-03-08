## Context

使用者的 offer 掛出後排在 order book 中，排隊位置決定成交速度。如果自身 rate 高於市場主流，排在後面成交機率低。Queue Position Strategy 估算排隊深度，並在排隊過深時主動降低利率或建議取消舊 offer 重掛。

## Goals / Non-Goals

**Goals:**
- 估算自身 offer 在 ask side 的排隊深度（rate 以下累積多少資金）
- 根據排隊深度與 ask depth 比例評估成交前景
- 排隊過深時計算利率折讓以改善位置
- 標記等待過久的 active offers 建議取消重掛
- 純計算，滿足 Apply 介面

**Non-Goals:**
- 不追蹤歷史成交速率（stateless 計算）
- 不做實際取消操作（只輸出 Cancels 建議）
- 不考慮隱藏單影響（使用可見深度）

## Decisions

### 1. 排隊深度估算

```
queueDepth = askDepth × (rate - bestAsk) / spread
```

rate 越高於 bestAsk，排在越後面。用 spread 正規化成排隊資金量。

如果 spread ≤ 0 或 askDepth ≤ 0，退回無數據模式。

### 2. 排隊比例與折讓

| 排隊比例 (queueDepth / askDepth) | 動作 |
|---|---|
| <= 20% | 正常，不調整 |
| 20-50% | 利率折讓 -2%（靠近前排） |
| > 50% | 利率折讓 -5%（積極改善位置） |

### 3. 過期 Offer 取消建議

Active offers 中，rate 高於 bestAsk + 2×spread 的視為「遠離市場」，加入 Cancels 建議。

### 4. Amount 不調整

Queue Position 只影響 rate 和 cancels，amount 保持不變。

## Risks / Trade-offs

- **[排隊深度為近似值]** → 實際 order book 是離散的，連續模型是簡化，但足夠做決策
- **[取消門檻硬編碼]** → 後續可參數化
- **[不考慮 fill rate]** → 需要歷史數據，超出 stateless 範疇
