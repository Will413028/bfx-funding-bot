## Context

Market Feed Service 是放貸引擎的共享市場數據層。它在獨立 goroutine 中運行，接收 B1 WebSocket Client 推送的即時數據，計算信號與體制，組裝成 MarketSnapshot 後透過 Go channel 廣播給 Worker Pool，同時快取到 Redis 供 Dashboard API 讀取。

架構文件定義的主循環 phases：
- Phase 0: Pre-flight（冷啟動、凍結檢查）
- Phase 1: 接收 WS 公開數據（ticker、book、trades）
- Phase 1.5: 閃崩偵測
- Phase 2: 信號計算（委託 SignalSource 模組）
- Phase 3: 體制識別
- Phase 3.5: 組裝 MarketSnapshot + 廣播

## Goals / Non-Goals

**Goals:**
- Market Feed Service 框架：Start/Stop 生命週期 + 主循環
- WebSocket 數據接收：透過 callback 累積最新 ticker、book、trades
- Snapshot 組裝：從原始數據計算 OrderBookSummary、偵測 WallPosition
- Flash Crash Detection：利率急跌偵測 + FlashFreeze 凍結
- 信號模組介面：定義 SignalSource interface，C1 不實作具體信號（placeholder）
- 透過 Go channel 廣播 MarketSnapshot

**Non-Goals:**
- 不實作具體信號計算模組（C3）
- 不實作 Order Book 進階分析（dust filter、hidden ratio、competitor detection）（C4）
- 不實作 Regime Detection 演算法（C5，C1 用 placeholder）
- 不將 Market Feed 整合進 `cmd/server/main.go`（Phase E 的 lending/service.go 負責）

## Decisions

### 1. 數據收集模式：Event-Driven Accumulation

WSClient 透過 callback 推送數據，Market Feed 在記憶體中維護最新狀態：
- `latestTicker`：最後一筆 ticker
- `bookEntries`：完整 order book（snapshot 覆蓋 + update 增量）
- `recentTrades`：最近 N 筆成交（ring buffer，保留 5 分鐘）

定期（預設 3 秒）從最新狀態組裝 MarketSnapshot。

替代方案：
- 每次 WS 訊息觸發重新計算：頻率太高，浪費 CPU
- 純 REST 輪詢：延遲高，已被取代

### 2. Snapshot 組裝週期：3 秒

3 秒的平衡點：
- 比 REST 3 分鐘快 60 倍
- 不會過度消耗 CPU
- 足以滿足策略決策需求

### 3. Flash Crash Detection 設計

監控指標：
- FRR 日變化百分比 > 閾值（預設 -30%）
- 最近 N 筆成交的加權平均利率 vs 當前 FRR 偏離度

觸發 FlashFreeze 後：
- MarketSnapshot.FlashFreeze = true
- 持續偵測直到指標回歸正常
- 自動解凍（cooldown 期間後指標正常即解凍）

### 4. Order Book 管理

維護完整 order book state：
- 收到 snapshot → 完全覆蓋
- 收到 update → 根據 COUNT 新增/更新/刪除
  - COUNT > 0: 新增或更新該價位
  - COUNT = 0: 刪除該價位（AMOUNT=1 offer side, AMOUNT=-1 bid side）

### 5. WallPosition 偵測

簡單閾值法：單一價位金額超過整體 book 該側的一定比例（預設 5%）即視為 wall。

### 6. 廣播機制

- 主要：Go channel (`chan *domain.MarketSnapshot`, buffer=16)
- 輔助：`repository.SnapshotCache.Set()`（快取到 Redis 供 Dashboard 讀取）
- 未來：`repository.SnapshotPubSub.Publish()`（多機廣播，Phase E 整合時啟用）

## Risks / Trade-offs

- **[Risk] WebSocket 斷線期間無新數據** → 重連後自動重新訂閱，斷線期間不產出 snapshot
- **[Risk] Flash Crash 閾值過敏感/遲鈍** → 提供可配置參數，後續根據歷史數據調整
- **[Trade-off] 3s 固定週期 vs 事件驅動** → 固定週期較簡單、CPU 消耗可預測，犧牲少量即時性
