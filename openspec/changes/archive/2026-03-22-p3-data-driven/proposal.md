## Why

P0-P2 完成後，引擎有了 composite pipeline、參數調校、bestAsk 定價、FRR 趨勢、sigmoid queue、cascade phasing。P3 聚焦於**數據驅動決策** — 用歷史分佈和均值回歸模型取代靜態閾值，並優化 credit 到期管理以減少資金空轉。

## What Changes

- **G14**：credit 到期時走 composite pipeline 重新定價，取代原有的沿用 rate renew
- **G16**：掃描 order book 找 rate 空隙，在無競爭者的位置報價
- **S4**：新增 RatePercentile 信號 — 當前利率在 7d 分佈的百分位
- **S10**：P(higher_rate) 用均值回歸公式精確計算，用於 EV_wait 決策
- **M2**：追蹤成交空檔時間 + 到期前預排程，減少 capital downtime

## Capabilities

### New Capabilities

- `rate-percentile-signal`: S4 當前利率在歷史分佈中的百分位信號
- `mean-reversion-model`: S10 均值回歸 P(higher_rate) 計算
- `gap-cost-tracking`: M2 成交空檔追蹤 + 到期前預排程
- `orderbook-gap-detection`: G16 order book rate 空隙偵測

### Modified Capabilities

- `composite-strategy`: 整合 percentile、gap detection、mean reversion、gap cost
- `lending-strategy`: G14 auto-renew re-pricing

## Impact

- **新增檔案**：`signal/percentile.go`、`strategy/meanreversion.go`、`orderbook/gap.go`
- **修改**：`execution/credit.go`（G14）、`strategy/composite.go`、`strategy/period.go`（M2 gap cost）、`worker/worker.go`（M2 追蹤）、`domain/snapshot.go`（新增 RatePercentile 欄位）
- **Domain 變更**：`DecisionContext` 新增 `AvgGapMinutes float64`；`MarketSnapshot` 新增 `RatePercentile float64`
