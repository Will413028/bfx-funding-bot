## Context

Bitfinex funding offer 可以被部分成交，剩餘部分繼續掛在 order book。小額殘留（如原本 5000 被吃到剩 30）佔用掛單額度但成交機率極低。Partial Fill Strategy 清理這些殘留並根據成交狀況調整策略。

## Goals / Non-Goals

**Goals:**
- 偵測 active offers 中的小額殘留（amount < minBalance）並建議取消
- 統計已有 offer 的平均填充率作為市場活躍度指標
- 填充率高時增加掛單量（市場活躍），低時減少
- 純計算，滿足 Apply 介面

**Non-Goals:**
- 不追蹤歷史成交記錄（stateless，只看當前 active offers）
- 不做實際取消操作（只輸出 Cancels 建議）
- 不區分部分成交 vs 原本就掛小額

## Decisions

### 1. 殘留偵測

Active offers 中 `amount < minBalance (50 USD)` 的視為殘留，加入 Cancels。

### 2. 填充率估算

由於 FundingOffer 沒有 `OriginalAmount` 欄位，無法直接算填充率。改用 active offers 的平均金額與 config.Amount.Max 的比值作為近似指標：

```
avgOfferSize = sum(activeOffers.Amount) / len(activeOffers)
fillProxy = 1 - (avgOfferSize / config.Amount.Max)
```

fillProxy 高 → offer 被吃得多 → 市場活躍。無 active offers 時 fillProxy = 0。

### 3. 金額調整

| fillProxy 範圍 | 動作 |
|---|---|
| < 0.3 | 市場不活躍，縮減至 80% |
| 0.3 - 0.7 | 正常，不調整 |
| > 0.7 | 市場活躍，放大至 120% |

最終 amount 仍受 config.Amount.Max 上限。

## Risks / Trade-offs

- **[fillProxy 為近似值]** → 沒有原始金額，用平均值推估，準確度有限但足夠指導方向
- **[門檻硬編碼]** → 後續可參數化
