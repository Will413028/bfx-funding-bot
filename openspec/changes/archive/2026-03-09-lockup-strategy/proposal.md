## Why

D3 Period Strategy 決定天數，但沒有考慮「鎖倉的機會成本」。長天數鎖定利率的同時，也喪失了在更好利率出現時重新部署資金的機會。Lockup Cost 計算一個折扣係數，當天數越長、市場越不確定時，要求更高的利率補償，否則應該縮短天數。這讓利率和天數的決策形成閉環。

## What Changes

- 新增 `lending/strategy/lockup.go`：Lockup Cost 模組
  - 鎖倉機會成本折扣：天數越長 → 折扣越大 → 需要更高利率才值得
  - 波動率加權：高波動時機會成本更高（未來利率變動的可能性更大）
  - 輸出：折扣後的最低可接受利率（effective floor），或建議縮短天數
- 新增完整單元測試

## Capabilities

### New Capabilities
- `lockup-strategy`: 基於鎖倉天數和市場波動率計算機會成本折扣的策略模組

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/lockup.go`, `lending/strategy/lockup_test.go`
- 純計算模組，只依賴 `domain/`
