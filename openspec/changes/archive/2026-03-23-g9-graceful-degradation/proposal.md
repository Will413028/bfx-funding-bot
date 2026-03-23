## Why

放貸引擎目前的 6 個信號源（Book Consumption、Liquidation、Margin、Momentum、CrossCurrency、Intraday）沒有健康度追蹤。任一信號源斷線或延遲時，MDC 僅靠 freshness decay 自然衰減，無法主動偵測故障、重新分配權重、或在恢復時安全地重新整合信號。在真金白銀的放貸場景中，這會導致策略決策基於不完整或過時的市場資訊。

## What Changes

- 新增信號健康度追蹤（Healthy / Warning / Degraded 三態），基於距離上次更新的心跳數
- MDC 聚合器在計算前檢查各信號健康狀態，故障信號排除並重分配權重
- 特定信號故障時啟用對應降級策略（如 Order Book 故障 → 純 FRR 模式）
- 信號恢復後需通過 2 心跳穩定確認才重新整合
- MarketSnapshot 新增信號健康度摘要，供 Worker 與 Dashboard 使用

## Capabilities

### New Capabilities
- `signal-health`: 信號源健康度追蹤 — 三態偵測（healthy/warning/degraded）、per-signal 最後更新時間戳、降級策略對應表
- `mdc-degradation`: MDC 降級模式 — 故障信號排除、權重重分配、Order Book 故障時 FRR-only fallback
- `signal-recovery`: 信號恢復確認 — 2 心跳穩定確認機制、恢復後權重漸進回升

### Modified Capabilities

## Impact

- `lending/signal/mdc.go` — MDC 聚合器新增健康度檢查 + 權重重分配邏輯
- `lending/marketfeed/service.go` — 信號計算後更新健康追蹤器、將健康摘要注入 snapshot
- `domain/snapshot.go` — MarketSnapshot 新增 `SignalHealth` 欄位
- `domain/signal.go` — 新增 `SignalHealthState` 型別 + `SignalHealthSummary` struct
- 前端 Dashboard 可選讀取健康度資訊（非本次範圍，僅預留欄位）
