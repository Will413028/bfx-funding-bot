## 1. Partial Strategy 核心

- [x] 1.1 新增 `lending/strategy/partial.go`：實作 `PartialStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作殘留偵測（amount < minBalance → Cancels）
- [x] 1.3 實作 fillProxy 計算（1 - avgOfferSize / Amount.Max）
- [x] 1.4 實作三級金額調整（<0.3 縮 80%, 0.3-0.7 不調, >0.7 放 120%）
- [x] 1.5 實作 flash freeze 和 insufficient balance 保護

## 2. 測試

- [x] 2.1 測試殘留偵測與取消建議
- [x] 2.2 測試 low activity 金額縮減
- [x] 2.3 測試 normal activity 不調整
- [x] 2.4 測試 high activity 金額放大（含 cap）
- [x] 2.5 測試無 active offers（fillProxy=0）
- [x] 2.6 測試 flash freeze 和 insufficient balance
