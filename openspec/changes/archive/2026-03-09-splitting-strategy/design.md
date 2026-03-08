## Context

D5 Allocation 分層後，每層可能有數千 USD 的掛單。在淺薄的 funding market 中，一筆大單佔 ask depth 比例過高會被其他參與者偵測，導致利率被壓低。Splitting 根據 order book 深度將大額掛單拆成多筆。

## Goals / Non-Goals

**Goals:**
- 根據 ask depth 計算單筆最大金額
- 將超過門檻的金額均勻拆分
- 每筆拆分後 >= 50 USD
- 保留原始利率和天數不變

**Non-Goals:**
- 不改變利率或天數（其他策略模組的職責）
- 不做時間錯開（D10 Stagger 的職責）
- 不分析競爭者行為來調整拆分（競爭者偵測已在 C4 完成，此處只用深度）

## Decisions

### 1. 單筆最大金額

```
maxPerOrder = askDepth × depthFraction (default 5%)
```

意義：單筆掛單不超過 ask side 總深度的 5%，避免形成明顯的 wall。

如果 askDepth 為零（無數據），不拆分。

### 2. 拆分邏輯

```
if amount <= maxPerOrder:
    return [原始掛單]  // 不需拆分

splitCount = ceil(amount / maxPerOrder)
perSplit = amount / splitCount
```

拆分後每筆金額相等。如果 `perSplit < 50`，減少 splitCount 直到每筆 >= 50。

### 3. 拆分上限

最多拆成 5 筆，避免產生過多掛單消耗 API 配額。

### 4. Apply 介面

Splitting 的 Apply 輸入是一個 DecisionContext，輸出是拆分後的多筆 OfferDecision。在 orchestrator 中，Splitting 會對 Allocation 的每層結果逐一處理。

為了獨立使用，Apply 接收 ctx 並基於 ctx.Available 和 order book 做拆分。

## Risks / Trade-offs

- **[5% depth fraction 可能太保守]** → 在深度充足的市場（如 fUSD）中，5% 已經足夠大；淺薄市場中保守是正確的
- **[均勻拆分暴露模式]** → 未來可加入 noise（D13）做金額擾動
