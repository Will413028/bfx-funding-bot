## 1. Noise Strategy 核心

- [x] 1.1 新增 `lending/strategy/noise.go`：實作 `NoiseStrategy` struct 和 `Apply` 方法
- [x] 1.2 實作利率隨機擾動（±1%）
- [x] 1.3 實作金額隨機擾動（±2%）
- [x] 1.4 實作心理價位避讓（0.0005 倍數偏移）
- [x] 1.5 實作 bounds clamping 和 flash freeze / insufficient balance 保護

## 2. 測試

- [x] 2.1 測試利率擾動在 ±1% 範圍內
- [x] 2.2 測試金額擾動在 ±2% 範圍內
- [x] 2.3 測試心理價位避讓
- [x] 2.4 測試 bounds clamping
- [x] 2.5 測試 flash freeze 和 insufficient balance
