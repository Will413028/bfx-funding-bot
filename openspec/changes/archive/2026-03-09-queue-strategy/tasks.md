## 1. Queue Strategy 核心

- [x] 1.1 新增 `lending/strategy/queue.go`：實作 `QueueStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作排隊深度估算（askDepth × (rate - bestAsk) / spread）
- [x] 1.3 實作三級折讓（<=20% 不調整, 20-50% -2%, >50% -5%）
- [x] 1.4 實作 stale offer 取消建議（rate > bestAsk + 2×spread）
- [x] 1.5 實作 no data、flash freeze、insufficient balance 保護

## 2. 測試

- [x] 2.1 測試 low queue 不調整
- [x] 2.2 測試 moderate queue -2% 折讓
- [x] 2.3 測試 deep queue -5% 折讓
- [x] 2.4 測試 stale offer 取消建議
- [x] 2.5 測試 no data（zero spread / zero depth）
- [x] 2.6 測試 flash freeze 和 insufficient balance
