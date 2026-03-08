## Context

前面的策略模組（D1-D4）各自處理利率和天數，但只輸出單筆掛單。在實際交易中，將資金集中在一筆掛單上有風險：如果利率不理想，全部資金可能閒置或被鎖定在差的利率。分層部署可以對沖這個風險。

## Goals / Non-Goals

**Goals:**
- 根據資金量決定拆分層數（1-3 層）
- 每層分配不同比例的資金
- Regime 影響分層策略（crisis 集中、contango 分散）
- 每筆分配的 amount 必須 >= 50 USD（Bitfinex 最低限額）

**Non-Goals:**
- 不決定每層的具體利率（那是 Pricing 的職責，由 orchestrator 組合）
- 不處理掛單拆分的深度感知（D6 Splitting 的職責）
- 不管理已有掛單的再平衡

## Decisions

### 1. 分層門檻

| 可用資金 | 層數 | 說明 |
|---------|------|------|
| < 150 | 1 | 不夠分（每層至少 50） |
| 150 - 1000 | 2 | 核心 70% + 探索 30% |
| > 1000 | 3 | 核心 50% + 穩健 30% + 探索 20% |

### 2. 各層定位

- **核心層（core）**：使用基礎利率（config.Rate.Min 附近），優先成交
- **穩健層（moderate）**：使用中間利率，平衡成交率和收益
- **探索層（aggressive）**：使用較高利率，捕捉高利率成交機會

利率的具體值由 Pricing Strategy 決定；Allocation 只負責金額分配和利率偏移方向（以 rate multiplier 表示）。

### 3. Regime 調整分層

- **Crisis**：強制 1 層，全部用最低利率（保護模式）
- **Backwardation**：最多 2 層，減少激進部分
- **Contango**：允許最大分層（有利市場，值得分散探索）
- **Neutral**：正常分層

### 4. 輸出格式

每層輸出一個 `OfferDecision`，amount 是分配金額，rate 為 baseRate × layerMultiplier：
- Core: ×1.0
- Moderate: ×1.1
- Aggressive: ×1.25

Period 由 Period Strategy 決定（此處用 config 預設值）。

## Risks / Trade-offs

- **[分層增加掛單數量]** → 最多 3 筆，API 配額影響小
- **[小額帳戶不分層]** → 設計意圖，避免拆分出低於 50 USD 的掛單
