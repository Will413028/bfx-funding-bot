## Context

Lending engine 的 signal aggregation（MDC 權重、decay λ）、regime detection（enter/exit 門檻）、pricing（premium 係數、deviation guard）目前全部使用硬編碼常數，對所有幣別一視同仁。

現有架構：
- `MDCAggregator` 在 `NewMDCAggregator()` 中初始化 `defaultWeights` / `defaultLambda`（全域 map）
- `RegimeDetector` 在 `NewRegimeDetector(cfg)` 中可接受 `RegimeConfig`，但目前所有 worker 都傳 nil（使用預設值）
- `PricingStrategy` 的 `Compute()` 直接讀取 package-level 常數（`maxPremiumUp`、`regimeContangoMul` 等）
- Worker 從 `StrategyConfig.Currency` 取得幣別，但僅用於 Bitfinex API 呼叫，不影響 signal/strategy 邏輯

## Goals / Non-Goals

**Goals:**
- 讓 stablecoin（fUSD/fUST）和 crypto（fBTC/fETH）使用不同的 signal 權重、regime 門檻和 pricing 參數
- 參數集以 preset 形式管理，不要求使用者手動配置每個參數
- 保持向後相容：不改 DB schema，不改 API contract

**Non-Goals:**
- 不做完全自訂參數 UI（未來可選 ParameterOverrides，但本次不做前端）
- 不做 per-currency 的獨立 market feed 或 signal pipeline（共用同一個 snapshot）
- 不做動態權重學習（G13 範疇）

## Decisions

### 1. CurrencyPreset 結構放在 domain 層

**決定：** 在 `domain/` 新增 `CurrencyPreset` struct，包含所有可調參數。在 `domain/` 新增 `CurrencyPresets` map 提供兩組預設值。

**理由：** domain 層是所有其他層的依賴，signal/strategy/worker 都能直接使用。避免循環依賴。

**替代方案：** 放在 appconfig 或 lending/config — 但這些層級太高或太窄，且 CurrencyPreset 是 domain concept。

### 2. 注入式而非查表式

**決定：** MDCAggregator、RegimeDetector、PricingStrategy 的建構子改為接受 `CurrencyPreset` 參數。Worker 初始化時根據 currency 選擇 preset 並注入。

**理由：**
- 測試友好：可傳入自訂 preset
- 無全域狀態：不同 worker 可用不同 preset
- 向後相容：現有 `NewMDCAggregator()` 保留為 `NewMDCAggregator()` 使用 stablecoin 預設

**替代方案：** 每次 `Aggregate()`/`Compute()` 都傳 currency 查表 — 增加每次呼叫的 overhead 且 API 不乾淨。

### 3. 預設值分類：stablecoin vs crypto

**決定：** 基於 strategy_specification.md § 12.7 的分類：

| 參數 | Stablecoin (fUSD/fUST) | Crypto (fBTC/fETH) |
|------|------------------------|---------------------|
| BookConsumption weight | 0.15 | 0.30 |
| Liquidation weight | 0.25 | 0.20 |
| MarginUsage weight | 0.25 | 0.15 |
| Momentum weight | 0.15 | 0.15 |
| CrossCurrency weight | 0.10 | 0.10 |
| Intraday weight | 0.10 | 0.10 |
| Regime enter threshold | 0.40 | 0.25 |
| Regime exit threshold | 0.25 | 0.15 |
| MaxPremiumUp | 0.40 | 0.60 |
| MaxPremiumDown | 0.15 | 0.25 |
| DeviationContango | 0.35 | 0.50 |
| DeviationBackwardation | 0.15 | 0.30 |

**理由：** Stablecoin 流動性深、book 信號弱，需要更大的 regime 門檻避免假突破。Crypto 波動大、book 信號強，需要更快反應和更大的 premium 空間。

### 4. 不改 DB schema

**決定：** `user_configs.config` 已是 JSONB，CurrencyPreset 作為 domain 層的 runtime 邏輯，不需要存 DB。未來若支援使用者自訂 override，可在 StrategyConfig 加入 optional 欄位。

**替代方案：** 新增 `currency_parameters` table — 過早抽象，目前只有兩組預設。

## Risks / Trade-offs

- **[Risk] 修改 MDCAggregator/RegimeDetector/PricingStrategy 的建構子是 breaking change** → Mitigation：保留無參數版本作為 default，新增 `WithPreset(preset)` 的 functional option 或直接加參數（internal package，只影響 lending 內部）
- **[Risk] 硬編碼預設值可能不是最佳** → Mitigation：基於 strategy_specification.md 的研究數據，可在 G13（fill rate tracking）完成後用歷史數據微調
- **[Risk] 測試需要同時覆蓋兩組 preset** → Mitigation：每個修改的模組新增 crypto preset 的測試 case
