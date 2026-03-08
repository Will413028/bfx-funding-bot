## 1. Domain Types

- [x] 1.1 新增 `domain/decision.go`：定義 `DecisionContext`、`DecisionResult`、`OfferDecision` types
- [x] 1.2 驗證 domain types 編譯通過（go build）

## 2. Pricing Strategy 核心

- [x] 2.1 新增 `lending/strategy/pricing.go`：實作 `PricingStrategy` struct 和 `Apply` 方法
- [x] 2.2 實作 MDC → 溢價乘數映射（線性插值）
- [x] 2.3 實作 base rate 選擇（FRR 優先，fallback to mid-rate）
- [x] 2.4 實作 regime 調整（contango +5%, backwardation -10%, crisis 用 min rate）
- [x] 2.5 實作 order book 壓力微調（bid/ask depth ratio）
- [x] 2.6 實作 wall avoidance 調整
- [x] 2.7 實作 rate clamping (config.Rate.Min ~ config.Rate.Max)
- [x] 2.8 實作 flash freeze 和 insufficient balance 保護

## 3. 測試

- [x] 3.1 測試 MDC 溢價映射：正值、負值、零值
- [x] 3.2 測試 base rate：FRR 可用 vs FRR 為零 fallback
- [x] 3.3 測試 regime 調整：contango、backwardation、crisis、neutral
- [x] 3.4 測試 order book 壓力調整
- [x] 3.5 測試 wall avoidance
- [x] 3.6 測試 rate clamping（下限、上限、範圍內）
- [x] 3.7 測試 flash freeze 返回空結果
- [x] 3.8 測試 insufficient balance 返回空結果
