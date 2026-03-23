## 1. Domain 層 — CurrencyPreset 型別

- [x] 1.1 在 `domain/` 新增 `currency_preset.go`，定義 `CurrencyPreset` struct
- [x] 1.2 定義 `StablecoinPreset`（匹配原始硬編碼值）和 `CryptoPreset`（新的激進參數）
- [x] 1.3 實作 `PresetForCurrency(currency string) CurrencyPreset`：USD/UST → stablecoin, BTC/ETH → crypto, 其他 → stablecoin fallback
- [x] 1.4 寫測試：TestPresetForCurrency + TestPresetWeightsSum

## 2. Signal 層 — MDCAggregator 參數化

- [x] 2.1 移除 hardcoded `defaultWeights`/`defaultLambda`，改用 CurrencyPreset
- [x] 2.2 新增 `NewMDCAggregatorWithPreset(preset CurrencyPreset)` 建構子
- [x] 2.3 保留 `NewMDCAggregator()` 向後相容（使用 StablecoinPreset）
- [x] 2.4 寫測試：TestMDC_CryptoPreset 驗證 crypto 權重效果更強
- [x] 2.5 更新既有 mdc_test.go 引用 `domain.StablecoinPreset.MDCWeights` 取代已刪除的 `defaultWeights`

## 3. Signal 層 — RegimeDetector 參數化

- [x] 3.1 新增 `NewRegimeDetectorWithPreset(preset CurrencyPreset)` 建構子
- [x] 3.2 `NewRegimeDetector(nil)` 改用 `StablecoinPreset` 的 enter/exit threshold
- [x] 3.3 寫測試：TestRegime_CryptoThresholds — score 0.30 觸發 crypto 的 contango 但不觸發 stablecoin 的

## 4. Strategy 層 — PricingStrategy 參數化

- [x] 4.1 將 package-level 常數（maxPremiumUp/Down, deviation guards）移為 struct field
- [x] 4.2 新增 `NewPricingStrategyWithPreset(preset CurrencyPreset)` 建構子
- [x] 4.3 保留 `NewPricingStrategy()` 向後相容
- [x] 4.4 寫測試：TestPricing_CryptoPreset — crypto 在 MDC=+1 時有更高的 rate
- [x] 4.5 新增 `ComputeBaseRateWithPreset()` + 保留 `ComputeBaseRate()` 向後相容

## 5. Worker 整合

- [x] 5.1 `CompositeStrategy` 新增 `preset` 欄位 + `NewCompositeStrategyWithPreset()`
- [x] 5.2 `CompositeStrategy.Apply()` 使用 `ComputeBaseRateWithPreset(snap, cfg, c.preset)`
- [x] 5.3 `factory.go` 的 `BuildWorkerDeps` 呼叫 `PresetForCurrency(currency)` 注入 preset 至 CompositeStrategy
- [x] 5.4 `marketfeed/service.go` 改為 per-symbol MDCAggregator 和 RegimeDetector（使用 CurrencyPreset）

## 6. 驗證

- [x] 6.1 `cd backend && go build ./...` 通過
- [x] 6.2 `cd backend && go test ./...` — 19/19 packages PASS
- [x] 6.3 `cd backend && golangci-lint run ./...` — 0 新增警告（14 pre-existing）
- [x] 6.4 更新 ROADMAP.md 標記 S7 完成
