## Why

P0（pipeline 接線）和 P1（快速收益 8 項）完成後，引擎的基礎收益已優化。P2 聚焦於**擇時能力**和 **fill rate** — 讓引擎更精準地判斷何時出手、以什麼利率出手、以及更快成交。這 7 項改進涵蓋新信號源（FRR 趨勢、rate spike）、定價模型重構（bestAsk 偏移）、和執行微調（wall positioning、sigmoid queue、cascade phasing、dynamic weekend）。

## What Changes

- **G12**：新增 FRR EMA 趨勢追蹤信號，影響 pricing 積極度
- **G15**：wall avoidance 改為 smart wall positioning（price just below wall）
- **GT6**：queue discount 從兩段式跳躍改為 sigmoid 曲線
- **S1**：定價模型從 FRR 乘數改為 bestAsk 相對偏移
- **S5**：清算瀑布分三階段回應（early/mid/late），取代統一 MDC=+1.0
- **S6**：weekend premium 從固定百分比改為歷史 ratio 動態計算
- **M5**：新增非清算性 rate spike 偵測器

## Capabilities

### New Capabilities

- `frr-trend-tracking`: G12 FRR EMA 趨勢信號
- `rate-spike-detection`: M5 非清算性利率飆升偵測

### Modified Capabilities

- `composite-strategy`: S1 bestAsk 定價 + S5 cascade phasing + S6 dynamic weekend
- `lending-strategy`: G15 smart wall + GT6 sigmoid queue

## Impact

- **新增檔案**：`signal/frrtrend.go`、`signal/ratespike.go`
- **重寫**：`strategy/pricing.go` ComputeBaseRate（S1 bestAsk 偏移）
- **修改**：`strategy/queue.go`（GT6 sigmoid）、`strategy/weekend.go`（S6 歷史 ratio）、`signal/liquidation.go`（S5 phase tracking）、`strategy/composite.go`（整合所有改動）
- **介面**：`MarketSnapshot` 可能需新增 `FRRTrend` 和 `RateSpike` 欄位
