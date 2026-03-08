## 1. Offer Execution 核心

- [x] 1.1 新增 `lending/execution/offer.go`：定義 consumer-side interfaces (FundingClient, KeyStore, Cipher, ExecutionLog)
- [x] 1.2 實作 `OfferExecutor` struct 和 `NewOfferExecutor` constructor
- [x] 1.3 實作 `ExecuteDecision(ctx, userID, decision)` 方法：API key 解密 → cancels → offers → 記錄
- [x] 1.4 實作 ExecutionSummary 回傳（成功/失敗計數）
- [x] 1.5 實作錯誤處理：API key 失敗中斷、單筆失敗繼續

## 2. 測試

- [x] 2.1 測試成功掛單 + 記錄
- [x] 2.2 測試成功撤單 + 記錄
- [x] 2.3 測試部分失敗繼續（cancel 失敗不中斷）
- [x] 2.4 測試部分失敗繼續（offer 失敗不中斷）
- [x] 2.5 測試 API key 不存在中斷
- [x] 2.6 測試空 decision 不執行
- [x] 2.7 測試 cancel 先於 offer 的順序
