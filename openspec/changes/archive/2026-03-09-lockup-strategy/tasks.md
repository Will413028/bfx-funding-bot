## 1. Lockup Strategy 核心

- [x] 1.1 新增 `lending/strategy/lockup.go`：實作 `LockupStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作鎖倉成本公式（baseCostPerDay × period × volAmplifier）
- [x] 1.3 實作 regime 放大（crisis ×2.0, backwardation ×1.5）
- [x] 1.4 實作利率溢價輸出（rate + lockupCost）
- [x] 1.5 實作天數縮短建議（lockupCost/rate > 20% 時）
- [x] 1.6 實作 flash freeze 和 insufficient balance 保護

## 2. 測試

- [x] 2.1 測試基礎鎖倉成本計算
- [x] 2.2 測試 regime 放大：crisis、backwardation、contango/neutral
- [x] 2.3 測試利率溢價輸出
- [x] 2.4 測試天數縮短（超過門檻 vs 未超過）
- [x] 2.5 測試 flash freeze 和 insufficient balance
