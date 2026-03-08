## 1. Impact Strategy 核心

- [x] 1.1 新增 `lending/strategy/impact.go`：實作 `ImpactStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作 impact ratio 計算（existing + new / askDepth）
- [x] 1.3 實作三級門檻（<=10% 不調整, 10-25% +3%, >25% +5% 且縮量）
- [x] 1.4 實作 zero depth 和 over_limit 處理
- [x] 1.5 實作 flash freeze 和 insufficient balance 保護

## 2. 測試

- [x] 2.1 測試 low impact 不調整
- [x] 2.2 測試 moderate impact +3% 溢價
- [x] 2.3 測試 high impact +5% 且縮量
- [x] 2.4 測試 over_limit（existing 已超標）
- [x] 2.5 測試 zero depth 跳過
- [x] 2.6 測試 flash freeze 和 insufficient balance
