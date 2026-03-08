## 1. Weekend Strategy 核心

- [x] 1.1 新增 `lending/strategy/weekend.go`：實作 `WeekendStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作溢價時間表（週五晚 +2%, 週六 +5%, 週日 +3%）
- [x] 1.3 實作 rate clamping 和 flash freeze / insufficient balance 保護

## 2. 測試

- [x] 2.1 測試工作日不調整
- [x] 2.2 測試週五晚間 +2%、週五早上不調整
- [x] 2.3 測試週六 +5%
- [x] 2.4 測試週日 +3%
- [x] 2.5 測試 rate clamping、flash freeze、insufficient balance
