## Context

`domain/regime.go` 已定義 4 種體制（contango / backwardation / neutral / crisis）和 `RegimeParams` struct。目前 `marketfeed/service.go` 的 `buildSnapshot` 在 Phase 3 硬編碼 `RegimeNeutral`。C3 signal modules 已完成 MDC aggregator，提供 `MDCResult.Score`（-1 到 +1）和 `DemandPressure / SupplyPressure`。Flash crash detector 已提供 `flashFreeze` bool。

依架構規範，`signal/` package 是純計算模組，只依賴 `domain/`。

## Goals / Non-Goals

**Goals:**
- 實作 `RegimeDetector`，基於 MDC score + 信號值 + ticker 數據判定體制
- 輸出完整 `RegimeParams`（volatility、trend、demand/supply ratio、duration）
- 體制轉換需要 hysteresis（防止在邊界頻繁切換）
- 整合至 marketfeed Phase 3

**Non-Goals:**
- 不做 regime prediction（只判定當前狀態，不預測未來）
- 不做複雜統計模型（如 Hidden Markov Model）——用簡單閾值法
- 不持久化 regime 歷史（記憶體中保留即可）

## Decisions

### 1. 體制判定邏輯

基於 MDC score 的閾值分類：
- **Crisis**: `flashFreeze == true` 或 MDC score 極端（|score| > 0.9 且 volatility 高）
- **Contango**: MDC score > +threshold（default 0.3）— 需求大於供給
- **Backwardation**: MDC score < -threshold（default -0.3）— 供給大於需求
- **Neutral**: |MDC score| <= threshold

### 2. Hysteresis 機制

進入體制的閾值（如 0.3）高於維持的閾值（如 0.2）。一旦進入 contango，需要 score 降到 0.2 以下才會退回 neutral，避免邊界抖動。

### 3. RegimeParams 計算

- **Volatility**: 基於 ticker 的 `DailyChangePerc` 的絕對值，結合近期波動歷史
- **TrendStrength**: 直接映射 MDC score（已經是 -1 到 +1）
- **DemandSupplyRatio**: `DemandPressure / max(SupplyPressure, epsilon)`
- **Duration**: 追蹤上次體制切換的時間

### 4. Struct 設計

`RegimeDetector` 是有狀態 struct（追蹤當前 regime、切換時間、volatility 歷史），暴露 `Detect(mdc, signals, ticker, flashFreeze, now)` 方法。

## Risks / Trade-offs

- **[Risk] 閾值不當** → 預設值保守，可通過 config 調整
- **[Trade-off] 簡單閾值 vs 統計模型** → 先用閾值法，Phase E 優化時可升級
