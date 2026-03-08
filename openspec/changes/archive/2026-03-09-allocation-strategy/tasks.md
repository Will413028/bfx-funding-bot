## 1. Allocation Strategy 核心

- [x] 1.1 新增 `lending/strategy/allocation.go`：實作 `AllocationStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作分層門檻（<150 → 1層, 150-1000 → 2層, >1000 → 3層）
- [x] 1.3 實作最低金額保護（每層 >= 50 USD）
- [x] 1.4 實作 tier rate multipliers（core ×1.0, moderate ×1.1, aggressive ×1.25）
- [x] 1.5 實作 regime 調整（crisis → 1層, backwardation → max 2層）
- [x] 1.6 實作 flash freeze 和 insufficient balance 保護

## 2. 測試

- [x] 2.1 測試小額單層、中額雙層、大額三層
- [x] 2.2 測試最低金額保護（分層降級）
- [x] 2.3 測試 rate multipliers 正確應用
- [x] 2.4 測試 regime 調整（crisis 單層、backwardation max 2層）
- [x] 2.5 測試金額分配比例正確
- [x] 2.6 測試 flash freeze 和 insufficient balance
