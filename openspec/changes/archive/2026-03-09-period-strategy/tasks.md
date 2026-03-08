## 1. Period Strategy 核心

- [x] 1.1 新增 `lending/strategy/period.go`：實作 `PeriodStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作 regime 驅動基礎天數（contango 75%, neutral 50%, backwardation 25%, crisis min）
- [x] 1.3 實作利率-天數線性縮放（rate/FRR ratio → ±30%）
- [x] 1.4 實作波動率縮短（volatility > 0.10 時打折）
- [x] 1.5 實作四捨五入 + clamp 到 config 範圍

## 2. 測試

- [x] 2.1 測試 regime 基礎天數：contango、neutral、backwardation、crisis
- [x] 2.2 測試利率縮放：高於 FRR、低於 FRR、FRR 為零
- [x] 2.3 測試波動率折扣：正常、高、極端
- [x] 2.4 測試 clamp 和 rounding
- [x] 2.5 測試 flash freeze 和 insufficient balance
