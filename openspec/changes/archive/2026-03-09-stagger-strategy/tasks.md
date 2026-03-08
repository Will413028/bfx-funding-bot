## 1. Stagger Strategy 核心

- [x] 1.1 新增 `lending/strategy/stagger.go`：實作 `StaggerStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作到期 bucket 計算（credits 按剩餘天數分桶）
- [x] 1.3 實作最佳 period 選擇（選到期金額最少的 day）
- [x] 1.4 實作集中度偵測（max bucket > 50% → concentrated）
- [x] 1.5 實作 flash freeze 和 insufficient balance 保護

## 2. 測試

- [x] 2.1 測試到期分佈有空缺 → 填補空缺
- [x] 2.2 測試多個空缺日 → 選中間值
- [x] 2.3 測試無 credits → 使用預設中間 period
- [x] 2.4 測試集中度偵測（concentrated vs balanced）
- [x] 2.5 測試 flash freeze 和 insufficient balance
