## 1. Floor Strategy 核心

- [x] 1.1 新增 `lending/strategy/floor.go`：實作 `FloorStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作機會成本地板（config.Rate.Min）
- [x] 1.3 實作 FRR 相對地板（FRR × 0.8）
- [x] 1.4 實作 regime 動態地板（crisis ×1.5, backwardation ×1.2）
- [x] 1.5 實作三層取最大值 + dominant source 追蹤
- [x] 1.6 實作 flash freeze 和 insufficient balance 保護

## 2. 測試

- [x] 2.1 測試機會成本地板為基準
- [x] 2.2 測試 FRR 相對地板：FRR 可用 vs FRR 為零
- [x] 2.3 測試 regime 調整：crisis ×1.5, backwardation ×1.2, contango/neutral ×1.0
- [x] 2.4 測試三層取最大值（各層分別勝出的場景）
- [x] 2.5 測試 dominant source reason 正確
- [x] 2.6 測試 flash freeze 返回空結果
- [x] 2.7 測試 insufficient balance 返回空結果
