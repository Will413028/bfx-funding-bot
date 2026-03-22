## Why

`factory.go` 目前只實例化 `PricingStrategy`，其餘 12 個策略模組（Floor、Period、Allocation、Splitting、Impact、Queue、Partial、Stagger、Weekend、Calendar、Lockup、Noise）已完成但未使用。引擎等於只發揮了 1/13 的策略能力——沒有 floor 保護、沒有資金分層、沒有 queue 調整、沒有週末溢價。這是目前收益的最大瓶頸，預期修復後 APY 提升 30-50%。

## What Changes

- 新增 `CompositeStrategy`，按 spec Appendix A Phase 4-5 的順序串連所有策略模組
- 每個策略模組新增 exported 純函數（如 `ComputeBaseRate()`），讓 composite 可以呼叫核心計算邏輯
- 修改 `factory.go` 使用 `NewCompositeStrategy()` 取代 `NewPricingStrategy()`
- 各策略的獨立 `Apply()` 方法保留不動（向後相容，可用於單元測試）

## Capabilities

### New Capabilities

- `composite-strategy`: CompositeStrategy 的 pipeline 編排邏輯——按順序執行 rate 解析、period 解析、資金結構、最終調整四個階段，產出完整的 DecisionResult

### Modified Capabilities

- `lending-strategy`: 每個策略模組新增 exported helper 函數，讓 composite 可以呼叫核心計算而不經過完整的 Apply() 流程

## Impact

- **涉及檔案**：`lending/factory.go`（修改）、`strategy/composite.go`（新增）、13 個 `strategy/*.go`（新增 exported helpers）
- **介面不變**：`worker.Strategy` interface、`DecisionContext`、`DecisionResult` 完全不動
- **風險**：pipeline 順序錯誤可能導致 rate 計算偏差。需要完整的整合測試
