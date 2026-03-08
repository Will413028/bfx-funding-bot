## 1. Interfaces

- [x] 1.1 建立 `lending/marketfeed/interfaces.go`：定義 SignalSource interface

## 2. Flash Crash Detection

- [x] 2.1 建立 `lending/marketfeed/flashcrash.go`：FlashCrashDetector struct + NewFlashCrashDetector
- [x] 2.2 實作 Check 方法：FRR 日變化百分比閾值偵測 + cooldown 機制

## 3. Snapshot Assembly

- [x] 3.1 建立 `lending/marketfeed/snapshot.go`：computeOrderBookSummary 函式
- [x] 3.2 實作 detectWallPositions 函式（閾值法偵測巨單牆）
- [x] 3.3 實作 assembleSnapshot 函式（整合所有數據組裝 MarketSnapshot）

## 4. Service Core

- [x] 4.1 建立 `lending/marketfeed/service.go`：Service struct + NewService constructor
- [x] 4.2 實作 WS callback handlers（ticker/book/trades 數據累積）
- [x] 4.3 實作 book state 管理（snapshot 覆蓋 + update 增量 + deletion）
- [x] 4.4 實作 Start 方法：連接 WS、訂閱頻道、啟動 snapshot loop
- [x] 4.5 實作 snapshot loop：定期組裝 + 廣播到 output channel + 快取到 Redis
- [x] 4.6 實作 Stop 方法：停止 loop、關閉 WS、關閉 channel

## 5. 測試

- [x] 5.1 建立 `lending/marketfeed/flashcrash_test.go`：正常/崩盤/cooldown 測試
- [x] 5.2 建立 `lending/marketfeed/snapshot_test.go`：OrderBookSummary/WallPosition/assembleSnapshot 測試
- [x] 5.3 建立 `lending/marketfeed/service_test.go`：數據累積 + snapshot 產出測試

## 6. 驗證

- [x] 6.1 確認完整專案編譯通過 + 所有測試通過
