## Why

D1 Pricing Strategy 計算出的利率可能在極端市場下跌破使用者可接受的最低報酬。Floor Strategy 提供一個基於機會成本的利率地板，確保掛單利率不會低於資金的替代用途報酬（如 DeFi yield、銀行利率）。這是策略決策層的安全閥——即使 MDC 和 regime 壓低利率，floor 保證使用者不會以虧損的利率出借。

## What Changes

- 新增 `lending/strategy/floor.go`：Floor Strategy 模組
  - 機會成本地板：使用者設定的最低可接受年化報酬率，轉換為日利率作為硬性下限
  - FRR 相對地板：以 FRR 為基準的比例下限（例如不低於 FRR × 80%），防止掛太低被市場吃掉
  - 動態地板：根據 regime 提高地板（crisis 時提高保護）
  - 輸出：計算後的地板利率，供後續策略模組使用
- 新增完整單元測試

## Capabilities

### New Capabilities
- `floor-strategy`: 基於機會成本框架計算利率地板的策略模組，確保掛單利率不低於資金替代用途的報酬

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/floor.go`, `lending/strategy/floor_test.go`
- 純計算模組，只依賴 `domain/`，不影響現有功能
- 與 D1 Pricing Strategy 互補：Pricing 計算目標利率，Floor 設定最低可接受利率
