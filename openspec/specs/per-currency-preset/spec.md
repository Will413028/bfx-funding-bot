## ADDED Requirements

### Requirement: CurrencyPreset 型別定義
系統 SHALL 定義 `CurrencyPreset` struct，包含以下可調參數群組：MDC 權重（6 個 signal type）、decay λ（6 個 signal type）、regime 門檻（enter/exit）、pricing 參數（maxPremiumUp/Down、regime 乘數、deviation guard）。

#### Scenario: CurrencyPreset 包含所有必要欄位
- **WHEN** 建立一個 CurrencyPreset 實例
- **THEN** 該實例 MUST 包含 MDCWeights (map[SignalType]float64)、MDCLambda (map[SignalType]float64)、RegimeEnterThreshold (float64)、RegimeExitThreshold (float64)、MaxPremiumUp (float64)、MaxPremiumDown (float64)、DeviationContango (float64)、DeviationBackwardation (float64)

### Requirement: 內建兩組幣別預設
系統 SHALL 提供 `StablecoinPreset` 和 `CryptoPreset` 兩組預設值。Stablecoin 適用於 fUSD/fUST（BookConsumption 權重 0.15、regime enter 0.40），Crypto 適用於 fBTC/fETH（BookConsumption 權重 0.30、regime enter 0.25）。

#### Scenario: fUSD 選擇 stablecoin preset
- **WHEN** currency 為 "USD" 或 "UST"
- **THEN** 系統 MUST 回傳 StablecoinPreset

#### Scenario: fETH 選擇 crypto preset
- **WHEN** currency 為 "BTC" 或 "ETH"
- **THEN** 系統 MUST 回傳 CryptoPreset

#### Scenario: 未知幣別 fallback
- **WHEN** currency 不在已知清單中
- **THEN** 系統 MUST 回傳 StablecoinPreset 作為安全預設

### Requirement: MDCAggregator 接受外部參數
MDCAggregator SHALL 支援透過建構子接收 CurrencyPreset 中的 MDCWeights 和 MDCLambda，取代硬編碼的 defaultWeights/defaultLambda。

#### Scenario: 使用 crypto preset 的 MDC 權重
- **WHEN** MDCAggregator 以 CryptoPreset 初始化
- **THEN** BookConsumption 權重 MUST 為 0.30（而非 stablecoin 的 0.15）

#### Scenario: 向後相容無參數建構
- **WHEN** MDCAggregator 以無參數方式建構（NewMDCAggregator()）
- **THEN** MUST 使用 StablecoinPreset 的預設值

### Requirement: RegimeDetector 接受外部門檻
RegimeDetector SHALL 從 CurrencyPreset 取得 enter/exit threshold，取代硬編碼常數。

#### Scenario: crypto 使用更敏感的 regime 門檻
- **WHEN** RegimeDetector 以 CryptoPreset 初始化
- **THEN** enter threshold MUST 為 0.25、exit threshold MUST 為 0.15

### Requirement: PricingStrategy 接受外部參數
PricingStrategy 的 premium 計算 SHALL 使用 CurrencyPreset 中的 maxPremiumUp/Down 和 deviation guard，取代硬編碼常數。

#### Scenario: crypto 有更大的 premium 空間
- **WHEN** PricingStrategy 以 CryptoPreset 計算價格
- **THEN** maxPremiumUp MUST 為 0.60（而非 stablecoin 的 0.40）

### Requirement: Worker 根據 currency 注入 preset
Worker 初始化時 SHALL 根據 StrategyConfig.Currency 自動選擇對應的 CurrencyPreset，並注入至 MDCAggregator、RegimeDetector、PricingStrategy。

#### Scenario: 啟動 fETH worker
- **WHEN** 建立一個 currency="ETH" 的 worker
- **THEN** 該 worker 的 MDCAggregator MUST 使用 CryptoPreset 的權重

#### Scenario: 重新載入 config 不改 currency
- **WHEN** worker 收到 ReloadConfig 且 currency 未變
- **THEN** preset MUST 保持不變，不重建 aggregator
