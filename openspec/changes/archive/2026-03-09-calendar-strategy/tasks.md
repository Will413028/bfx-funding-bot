## 1. Calendar Strategy 核心

- [x] 1.1 新增 `lending/strategy/calendar.go`：實作 `CalendarStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作月末事件偵測與溢價（最後 3 天 + 1 天後）
- [x] 1.3 實作季度交割偵測（3/6/9/12 月最後週五）與溢價
- [x] 1.4 實作事件前 period 縮短
- [x] 1.5 實作 flash freeze 和 insufficient balance 保護

## 2. 測試

- [x] 2.1 測試月末溢價（不同天數）
- [x] 2.2 測試季度交割溢價
- [x] 2.3 測試重疊時取較高溢價
- [x] 2.4 測試 period 縮短
- [x] 2.5 測試無事件不調整
- [x] 2.6 測試 flash freeze 和 insufficient balance
