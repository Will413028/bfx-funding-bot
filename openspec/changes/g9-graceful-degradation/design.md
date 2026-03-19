## Context

放貸引擎的 MDC 聚合器（`signal/mdc.go`）目前使用 freshness decay（`e^(-λ × age)`）讓過時信號自然衰減，但沒有主動偵測信號故障或執行降級策略。`marketfeed/service.go` 計算信號後直接傳入 MDC 聚合器，無健康追蹤層。

現有架構：
```
signalSources[].Compute(raw) → []SignalValue → mdcAgg.Aggregate(signals, now) → MDCResult
```

目標架構：
```
signalSources[].Compute(raw) → []SignalValue → healthTracker.Update(signals) → mdcAgg.Aggregate(signals, health, now) → MDCResult
```

## Goals / Non-Goals

**Goals:**
- 主動偵測信號源健康狀態（healthy/warning/degraded），基於心跳間隔
- 故障信號排除 + 剩餘信號權重重分配
- 特定信號故障啟用對應降級策略（§6.3 對照表）
- 恢復後 2 心跳穩定確認才重新整合
- MarketSnapshot 攜帶健康摘要，供下游消費

**Non-Goals:**
- 前端 Dashboard 健康度 UI 展示（僅預留 domain type）
- Signal Recovery Smoothing 的 3 步權重回升（G2，獨立項目）
- 自動調整心跳間隔或信號參數

## Decisions

### 1. SignalHealthTracker 作為獨立 struct，放在 `signal/` package

**選擇**：新增 `signal/health.go` 內的 `SignalHealthTracker` struct，由 `marketfeed/service.go` 持有並在每次 snapshot 構建時呼叫。

**替代方案**：嵌入 MDCAggregator 內部。但 MDC 聚合是純計算（不持有狀態），健康追蹤需要跨 tick 狀態，職責不同。

**理由**：符合現有 `signal/` package 的純計算模式，且 health tracker 只依賴 `domain/` 型別。

### 2. 健康判定基於固定心跳數，而非動態閾值

**選擇**：Warning = >2 heartbeats stale，Degraded = >5 heartbeats stale，心跳間隔由外部傳入（default 3s）。

**理由**：spec §6.3 明確定義固定心跳閾值，不需要動態調整。心跳間隔可配置以支援 dead water period（15min interval）。

### 3. MDCAggregator.Aggregate 新增 health 參數

**選擇**：擴展 `Aggregate` 簽名為 `Aggregate(signals, health, now)` — health 為 `map[SignalType]SignalHealthState`。

**替代方案**：在 Aggregate 前過濾 signals slice。但 spec 要求特定降級策略（如 liquidation 故障 → book consumption 權重增加到 35%），過濾無法表達權重覆蓋。

**理由**：讓 MDC 聚合器知道健康狀態，才能執行 spec 定義的特定降級策略。健康為 nil 或空 map 時行為不變（向後相容）。

### 4. Order Book 故障 = FRR-only mode，透過 MarketSnapshot 標記

**選擇**：新增 `snapshot.DegradedMode bool` + `snapshot.DegradedReason string`。Order Book 故障時 MDC score 強制為 0，worker 的 pricing strategy 看到 MDC=0 自然回到 FRR following。

**替代方案**：新增專用 FRR-only 模式在 worker 層。但現有 pricing.go 在 MDC=0 時已等於 FRR following（deviation guard 會確保不偏離），不需要額外模式。

**理由**：最小化改動，利用現有 pricing pipeline 行為。

## Risks / Trade-offs

- **[Risk] 健康追蹤增加 per-tick 計算** → 影響極小，僅 6 個 map 查詢 + 比較
- **[Risk] 信號恢復後立即被高 MDC 嚇到** → 2 心跳確認 + 未來 G2 的 recovery smoothing 緩解
- **[Trade-off] 向後相容 vs 乾淨 API** → 選擇新增 optional 參數（health 為 nil 時不影響現有行為），保留所有現有測試不改動
