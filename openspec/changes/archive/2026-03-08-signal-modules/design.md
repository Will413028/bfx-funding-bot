## Context

`marketfeed/interfaces.go` 定義了 `SignalSource` interface：`Name() string` + `Compute(data *domain.RawMarketData) domain.SignalValue`。每個信號模組實作此 interface，由 Market Feed Service 在 Phase 2 呼叫。

信號模組需要跨次呼叫維護狀態（歷史數據、窗口計算），因此用 struct 而非純函式。

## Goals / Non-Goals

**Goals:**
- 實作 5 個信號源 + 1 個 MDC 聚合器
- 每個模組純計算、僅依賴 `domain/`
- MDC 包含 freshness decay、recovery smoothing、liquidation hard override

**Non-Goals:**
- 不從外部 API 拉取數據（margin、cross-currency 數據在 RawMarketData 不足時用 placeholder）
- 不實作 weekly reweighting（Phase E 優化時再加）
- 不做持久化（信號狀態只在記憶體中）

## Decisions

### 1. 信號模組狀態管理

每個信號模組是一個 struct，維護滑動窗口歷史數據：
- `book.go`：保留最近 N 個 book snapshot 的 total depth，計算消耗速率
- `liquidation.go`：5 分鐘滑動窗口累積清算量
- `momentum.go`：5min/20min 滑動窗口的 VWAP
- `margin.go`：1 小時窗口的 long/short 變化
- `crossccy.go`：5 分鐘窗口的 funding rate 變化

### 2. MDC 聚合公式

```
MDC = tanh(Σ(Signal_i × EffectiveWeight_i))
EffectiveWeight_i = BaseWeight_i × e^(-λ_i × age_seconds_i)
```

Base weights：Book 25%, Liquidation 20%, Margin 20%, Momentum 15%, CrossCcy 10%, (Intraday 10% — placeholder)

### 3. Liquidation Hard Override

清算瀑布觸發時強制 MDC = +1.0，退場時 3-step regression（+0.7 → +0.3 → 0）。

### 4. 簡化實作策略

由於 `RawMarketData` 目前只包含 ticker + book + trades，部分信號源（margin、crossccy）的完整數據需要額外 API。C3 先實作核心邏輯，數據不足時降低 confidence 並輸出中性值。後續 Phase E 整合更多數據源。

## Risks / Trade-offs

- **[Risk] 初始啟動冷啟動** → 窗口填滿前輸出 confidence=0 的中性信號
- **[Trade-off] 精確度 vs 簡潔性** → 用近似方法（如 EMA 近似 VWAP），避免過度複雜
