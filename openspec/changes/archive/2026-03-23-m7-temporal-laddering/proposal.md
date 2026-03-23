## Why

CompositeStrategy 在每次 tick 時一次性部署所有可用資金，所有 offer 基於同一個 snapshot 的 bestAsk。但利率每分鐘都在變動——一次性全部部署等於賭單一時間點的利率，錯過更好的進場價格。

Temporal Laddering 將資金分三批跨三個心跳部署（類似 DCA），分散時間風險。在波動市場中，至少有一批可能捕捉到更好利率。

## What Changes

- Worker 新增 `trancheIndex`（0/1/2）追蹤目前在第幾批
- 每次 tick 時，Worker 將 `Available` 限縮為 `actual / (3 - trancheIndex)`，讓策略只看到 1/3 的可用餘額
- 策略本身不需改動——它只是看到更少的可用資金，自然只部署 1/3
- 三批部署完畢後 reset（或 config 變更時 reset）
- 當可用餘額太少（< minBalance × 2）時跳過 laddering，一次部署全部

## Capabilities

### New Capabilities

- `temporal-laddering`: 跨心跳 DCA 部署 — Worker 層級的三批次資金分配，每批使用最新 snapshot 重新計算最佳利率

### Modified Capabilities

## Impact

- `internal/lending/worker/worker.go` — 新增 trancheIndex 狀態 + tick 中的 Available 限縮邏輯
