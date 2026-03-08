## Why

D6 Splitting 拆分掛單降低單筆影響，但沒有評估使用者「全部掛單合計」對市場的衝擊。如果使用者的總掛單量佔 ask depth 比例過高，即使拆分了也會壓低市場利率。Market Impact 策略評估自身掛單對市場的衝擊程度，並在衝擊過大時縮減掛單量或提高利率以補償。

## What Changes

- 新增 `lending/strategy/impact.go`：Market Impact 模組
  - 自身衝擊比例：(自身掛單量 + 新增量) / ask depth
  - 衝擊溢價：衝擊比例超過門檻時，按比例提高利率以補償市場壓力
  - 量縮減：衝擊比例嚴重超標時，建議縮減掛單量
- 新增完整單元測試

## Capabilities

### New Capabilities
- `impact-strategy`: 評估自身掛單對市場的衝擊程度，調整利率和掛單量以減少市場壓力

### Modified Capabilities

## Impact

- 新增檔案：`lending/strategy/impact.go`, `lending/strategy/impact_test.go`
- 純計算模組，只依賴 `domain/`
