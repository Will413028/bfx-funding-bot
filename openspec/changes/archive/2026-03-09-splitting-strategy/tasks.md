## 1. Splitting Strategy 核心

- [x] 1.1 新增 `lending/strategy/splitting.go`：實作 `SplittingStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作深度感知最大金額（askDepth × 5%）
- [x] 1.3 實作均勻拆分 + 最小金額保護 + 最大拆分數 5
- [x] 1.4 實作 flash freeze 和 insufficient balance 保護

## 2. 測試

- [x] 2.1 測試深度感知最大金額：正常深度 vs 零深度
- [x] 2.2 測試拆分：超過上限 vs 未超過
- [x] 2.3 測試最小金額保護（拆分降級）
- [x] 2.4 測試最大拆分數 5 的上限
- [x] 2.5 測試 rate 和 period 保留
- [x] 2.6 測試 flash freeze 和 insufficient balance
