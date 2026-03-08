## Context

Phase C 完成了市場分析 pipeline，產出 `MarketSnapshot`，包含 MDC score（-1 到 +1 的市場需求曲線分數）、市場體制（contango/backwardation/neutral/crisis）、掛單簿摘要、牆位等資訊。現在需要建立策略決策層的基礎型別和第一個策略模組。

目前 `engine/strategy.go` 的 MVP 策略僅使用固定利率（`config.Rate.Min`），不考慮任何市場狀態。Pricing Strategy 將取代這個邏輯，根據市場狀態動態計算利率。

## Goals / Non-Goals

**Goals:**
- 定義 `DecisionContext` / `DecisionResult` domain types，作為所有 13 個策略模組的統一介面
- 實作 Pricing Strategy 模組：基於 MDC + regime + order book 計算建議利率
- 純計算、無 I/O、100% 單元測試覆蓋
- 隱式滿足 `worker.Strategy` interface（`Apply(*DecisionContext) *DecisionResult`）

**Non-Goals:**
- 不實作其他策略模組（floor、period、allocation 等屬於 D2-D13）
- 不修改現有 MVP engine（Phase E 才整合）
- 不擴充 `StrategyConfig`（目前 MVP config 足夠，完整參數化屬於後續）
- 不處理掛單執行邏輯（Phase E）

## Decisions

### 1. DecisionContext 聚合所有策略輸入

`DecisionContext` 包含策略模組決策所需的所有資訊：

```go
type DecisionContext struct {
    Snapshot      *MarketSnapshot  // 市場快照（Phase C 產出）
    Config        *StrategyConfig  // 使用者策略設定
    ActiveOffers  []FundingOffer   // 當前活躍掛單
    ActiveCredits []FundingCredit  // 當前活躍債權
    Available     float64          // 可用餘額
    Currency      string           // 幣種 (e.g. "fUSD")
}
```

**理由**：策略模組不做 I/O，所有需要的資料由 worker 預先拉取後組裝成 context 傳入。這保持 strategy package 的純計算特性。

### 2. DecisionResult 表達分層決策

```go
type DecisionResult struct {
    Offers       []OfferDecision  // 建議的掛單列表
    Cancels      []int64          // 建議取消的 offer IDs
    RenewCredits []int64          // 建議續約的 credit IDs
    Reason       string           // 決策理由（用於日誌）
}

type OfferDecision struct {
    Amount float64
    Rate   float64
    Period int
}
```

**理由**：Result 支援多筆掛單（splitting 策略需要），也支援取消和續約決策。後續模組可以用 pipeline 模式逐步修改 result。

**替代方案**：單一 offer 輸出 → 不夠彈性，splitting/allocation 需要多筆。

### 3. Pricing Strategy 三層溢價計算

利率計算分三層：

1. **Base Rate**：FRR（Flash Return Rate，市場參考利率）
2. **MDC Premium**：MDC score → 溢價乘數映射
   - MDC = +1（極度需求）→ multiplier = maxPremium (1.5)
   - MDC = 0（均衡）→ multiplier = 1.0
   - MDC = -1（極度供給）→ multiplier = minPremium (0.7)
   - 線性插值
3. **Regime Adjustment**：
   - Contango：+5% 加碼（趨勢向上）
   - Backwardation：-10% 減碼（趨勢向下）
   - Crisis：直接使用 config.Rate.Min（保守）
   - Neutral：不調整

最終利率 = clamp(baseRate × mdcMultiplier × regimeMultiplier, config.Rate.Min, config.Rate.Max)

**替代方案**：非線性映射（sigmoid）→ 線性更透明易調整，MVP 先用線性，後續可參數化。

### 4. Order Book 壓力微調

在三層計算後，根據掛單簿做最後微調：

- Bid/Ask depth ratio > 1.5（借方壓力大）：利率 +2%
- Offer wall 在建議利率附近：利率 -1%（避免排在牆後面）

微調幅度小且有上下限 clamp，不會大幅改變三層計算的結果。

### 5. FlashFreeze 處理

當 `Snapshot.FlashFreeze == true` 時，策略直接返回空 result（不掛單），保護使用者資金。

## Risks / Trade-offs

- **[線性映射過於簡化]** → 先 ship 再觀察，後續可替換為 sigmoid 或查表；MDC score 本身已經是 tanh 壓縮過的，疊加線性映射在合理範圍內
- **[硬編碼參數]** → Premium 範圍 (0.7-1.5)、regime 調整幅度 (±5-10%) 目前硬編碼為常數，後續 D 系列完成後統一參數化到 StrategyConfig
- **[單獨 pricing 模組暫時不完整]** → 沒有 floor、period、allocation 配合，暫時只產出利率決策，amount 和 period 用 config 預設值。這是設計上的預期——各模組逐步完成後由 orchestrator 串接
