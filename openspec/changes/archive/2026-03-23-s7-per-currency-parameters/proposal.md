## Why

目前所有幣別共用同一組 MDC 權重、regime 門檻和 premium 係數（全部硬編碼在 signal/mdc.go、signal/regime.go、strategy/pricing.go）。但 fUSD（低波動、高流動性）和 fETH（高波動、事件驅動）的市場特性完全不同——使用相同參數意味著對某一邊不是過度保守就是過度激進。Per-currency 參數是提升 yield 最直接的手段（預估 fETH 15-25% 提升）。

## What Changes

- 新增 `CurrencyPreset` 結構：封裝 MDC 權重、decay λ、regime 門檻、premium 係數、deviation guard 為一組可切換的參數集
- 內建兩組預設：`stablecoin`（fUSD/fUST）和 `crypto`（fBTC/fETH），基於 strategy_specification.md § 12.7
- MDCAggregator、RegimeDetector、PricingStrategy 改為接受 `CurrencyPreset` 參數，不再使用硬編碼常數
- Worker 初始化時根據 config.Currency 選擇對應的 preset，注入 signal/strategy pipeline
- StrategyConfig 可選新增 `ParameterOverrides` 欄位供進階使用者微調（不改 DB schema，因為是 JSONB）

## Capabilities

### New Capabilities

- `per-currency-preset`: 幣別參數預設集 — 定義 stablecoin/crypto 兩組預設參數集（MDC 權重、decay λ、regime 門檻、premium 係數），並透過 currency 自動選擇

### Modified Capabilities

## Impact

- `internal/domain/config.go` — 新增 CurrencyPreset 型別 + 預設值
- `internal/lending/signal/mdc.go` — MDCAggregator 接受外部權重/lambda
- `internal/lending/signal/regime.go` — RegimeDetector 接受外部門檻
- `internal/lending/strategy/pricing.go` — 接受外部 premium/deviation 參數
- `internal/lending/worker/worker.go` — 初始化時注入 currency preset
- `internal/lending/service.go` — workerFactory 傳遞 currency preset
