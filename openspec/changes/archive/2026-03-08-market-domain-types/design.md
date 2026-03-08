## Context

架構文件 (`backend_architecture.md`) 規劃了三個 domain 型別檔案：`snapshot.go`、`signal.go`、`regime.go`，標記為「待實作」。這些型別是 Phase C/D 所有模組的共同語言：

- `MarketSnapshot` 是市場分析層的核心輸出，包含所有聚合後的市場數據
- `SignalValue` 是信號模組的標準輸出格式
- `RegimeType` 是市場體制識別的結果

目前 `bitfinex/ws_types.go` 定義了 WebSocket 原始數據型別（FundingTicker、BookEntry、FundingTrade），屬於基礎設施層。Domain 層型別需要對這些原始數據做聚合和抽象。

## Goals / Non-Goals

**Goals:**
- 定義 Phase C/D 所需的所有 domain 型別
- 型別純粹、無外部依賴（遵守 domain 層規則）
- 提供足夠的 enum / 常數讓後續模組有明確的語義

**Non-Goals:**
- 不實作任何業務邏輯或計算函式
- 不定義 interface（那是 service/repository 層的事）
- 不涉及持久化（這些型別目前只在記憶體中使用）

## Decisions

### 1. RawMarketData 結構

包裝從 WebSocket 接收的原始數據，供信號模組消費：
- Ticker (最新 FundingTicker)
- Book (當前完整 order book entries)
- RecentTrades (最近一段時間的成交)
- Symbol + Timestamp

這是「快照時間點的原始市場狀態」，與聚合後的 `MarketSnapshot` 區分。

### 2. SignalType 枚舉

對應架構文件的 6 個信號源 + regime：
- MDC (Market Demand Curve)
- BookConsumption (Order Book 消耗速率)
- LiquidationCascade (清算瀑布)
- Momentum (雙速 VWAP)
- MarginUsage (保證金使用率)
- CrossCurrency (跨幣種相關性)

### 3. RegimeType 枚舉

基於市場供需狀態的體制分類：
- Contango (供不應求，利率上升)
- Backwardation (供過於求，利率下降)
- Neutral (平衡)
- Crisis (閃崩/極端波動)

### 4. 型別放置策略

全部放在 `domain/` 目錄，遵守既有慣例（純 struct + const，無 import 外部套件）。`time` 是標準庫，可以使用。

## Risks / Trade-offs

- **[Risk] 型別設計過早，後續實作時需要調整** → 這些是「最小必要型別」，後續模組可以按需擴充欄位
- **[Trade-off] 精簡 vs 完整** → 選擇精簡，只定義架構文件明確提到的型別，避免過度設計
