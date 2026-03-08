## Context

D1 Pricing Strategy 已實作完成，基於 MDC + regime + order book 計算目標利率，最後用 `config.Rate.Min` 做硬性 clamp。但 `config.Rate.Min` 是靜態的使用者設定，不會根據市場狀態調整。Floor Strategy 提供一個動態的、基於機會成本的利率地板，作為 Pricing 的補充。

在策略 pipeline 中，Floor 的位置是：Pricing 算出目標利率 → Floor 算出地板利率 → 最終利率 = max(目標利率, 地板利率)。

## Goals / Non-Goals

**Goals:**
- 計算動態利率地板，基於多個來源取最高值
- 純計算、無 I/O、隱式滿足 `Apply(*DecisionContext) *DecisionResult` 介面
- 可獨立測試，不依賴 Pricing Strategy

**Non-Goals:**
- 不整合進 Pricing Strategy（各模組獨立，由未來 orchestrator 串接）
- 不修改使用者的 StrategyConfig
- 不計算目標利率（那是 Pricing 的職責）

## Decisions

### 1. 三層地板取最高值

Floor 從三個來源各算一個地板，取最高值作為最終地板：

```go
floor = max(
    opportunityCostFloor,  // 使用者設定的機會成本年化 → 日利率
    frrRelativeFloor,      // FRR × frrFloorRatio (default 0.8)
    regimeFloor,           // 根據 regime 動態調整
)
```

**理由**：多層保護，每層針對不同風險。取最高值確保所有條件都被滿足。

### 2. 機會成本地板

使用 `config.Rate.Min` 作為使用者設定的最低可接受日利率。這已經隱含了使用者對機會成本的判斷。

未來可擴充 StrategyConfig 加入 `OpportunityCostAPY` 欄位做更精確的計算，但目前 `Rate.Min` 已足夠。

### 3. FRR 相對地板

`frrFloor = FRR × 0.8`

意義：不低於市場參考利率的 80%，防止掛單遠低於市場而被套利。

當 FRR 為零時（無市場數據），此層不生效。

### 4. Regime 動態地板

- Crisis：地板 = `config.Rate.Min × 1.5`（危機時提高保護，寧可不成交也不虧錢）
- Backwardation：地板 = `config.Rate.Min × 1.2`（下行市場，稍微提高保護）
- Contango / Neutral：不加碼（`config.Rate.Min × 1.0`）

### 5. Apply 輸出格式

Floor Strategy 的 `Apply` 返回一個 `DecisionResult`，其中 `Offers[0].Rate` 是計算出的地板利率。Reason 欄位說明哪個地板生效。

未來 orchestrator 會取 `max(pricing.Rate, floor.Rate)` 作為最終利率。

## Risks / Trade-offs

- **[FRR 80% 可能太保守]** → 硬編碼常數，後續可參數化到 StrategyConfig
- **[Crisis 加碼 1.5x 可能導致完全不成交]** → 這是設計意圖：危機時保護資金優先於成交
- **[與 Pricing 的 crisis 邏輯重疊]** → Pricing 在 crisis 時用 `config.Rate.Min`，Floor 在 crisis 時用 `config.Rate.Min × 1.5`，Floor 會勝出，這是正確行為
