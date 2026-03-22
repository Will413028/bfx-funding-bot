## Context

13 個策略模組各自實作 `worker.Strategy` interface（`Apply(ctx) → DecisionResult`），各自獨立產出完整的 offers/cancels。目前 `factory.go:50` 只接 `PricingStrategy`，其餘 12 個未使用。

每個策略負責不同面向：rate 定價、floor 保護、period 選擇、資金分層、order 拆分、queue 調整、market impact 管理、weekend/calendar 溢價、noise 擾動、到期分散、partial fill 管理、lockup 成本。

關鍵限制：所有策略都是**獨立創建者**（各自從 DecisionContext 產生完整結果），不是 sequential modifier（不讀取前一個策略的 output）。

## Goals / Non-Goals

**Goals:**
- 建立 `CompositeStrategy` 按 spec Appendix A Phase 4-5 順序串連 13 個策略模組
- 每個策略模組 export 核心計算邏輯為純函數，供 composite 呼叫
- `factory.go` 改用 `CompositeStrategy`，一行改動完成切換
- 保留所有策略的獨立 `Apply()` 方法，向後相容

**Non-Goals:**
- 不改 `worker.Strategy` interface
- 不改 `DecisionContext` / `DecisionResult` types
- 不新增 strategy 之間的依賴（每個 helper 仍是純函數）
- 不實作 G-Tune 參數調校（GT1-GT6 是獨立的後續工作）

## Decisions

### D1: Pipeline 模式 — 提取 helper 函數 + composite 編排

**選擇**：每個策略模組新增 exported 純函數，CompositeStrategy 按順序呼叫。

**替代方案考慮**：
- **Option A: Parallel merge**（各策略獨立 Apply，merge 結果）— 不可行，因為 13 個策略各自產出 rate/amount/period 都不同，無法合理 merge
- **Option B: 改 Apply 簽名為 chain pattern**（`Apply(ctx, prevResult)`）— 需改 interface，影響範圍太大
- **Option C: Composite 直接重寫所有邏輯** — DRY 違反，維護地獄

**結論**：Option D（helper 提取）是唯一不改 interface、不重複邏輯、且能正確組合的方式。

### D2: Pipeline 階段順序

依 spec Appendix A Phase 4-5，四階段：

```
Stage 1: Rate Resolution
  PricingStrategy.ComputeBaseRate()     → baseRate
  FloorStrategy.ComputeFloorRate()      → floorRate
  rate = max(baseRate, floorRate)
  rate *= WeekendPremium.ComputeMultiplier()
  rate *= CalendarEvent.ComputeMultiplier()
  rate += LockupCost.ComputePremium()     （可能縮短 period）
  rate *= QueuePosition.ComputeDiscount() （收集 stale cancels）
  rate *= MarketImpact.ComputePremium()   （可能縮量）
  rate = clamp(rate, Config.Rate.Min, Config.Rate.Max)

Stage 2: Period Resolution
  period = PeriodStrategy.ComputePeriod()
  period = StaggerStrategy.AdjustPeriod() （若有 active credits）

Stage 3: Offer Structure
  tiers = AllocationStrategy.ComputeTiers()
  for each tier:
    tier.rate = rate * tier.rateMultiplier
    splits = SplittingStrategy.ComputeSplits(tier)
    → append to offers

Stage 4: Final Adjustments
  for each offer:
    NoiseStrategy.ApplyNoise(&offer)
  PartialFillStrategy.ComputeAdjustment()   （amount 調整 + residual cancels）
```

### D3: Helper 函數命名和簽名慣例

每個策略的 exported helper 命名為 `Compute*` 或 `Apply*`：
- 純計算（無副作用）：`Compute*(inputs) → result`
- 原地修改：`Apply*(offer *OfferDecision)`

所有 helper 接收必要的原始參數（非完整 DecisionContext），保持純函數特性便於測試。

### D4: CompositeStrategy 的 guard checks

CompositeStrategy 在 pipeline 開頭統一做 guard checks（snapshot nil、flash freeze、balance < $50），不再由各 helper 重複檢查。各 helper 假設輸入已驗證。

## Risks / Trade-offs

- **[Pipeline 順序耦合]** → 階段順序直接影響最終 rate。透過完整的整合測試驗證，且順序固定在 composite.go 中集中管理
- **[Helper 函數簽名變動]** → 新增 exported 函數不影響既有 Apply()，但未來修改 helper 需同步更新 composite 的呼叫方。透過 compile-time 檢查自然防護
- **[PricingStrategy 的 wall avoidance 在 composite 中的位置]** → Wall avoidance 影響 rate，保留在 Stage 1 的 ComputeBaseRate 內（不額外提取）
