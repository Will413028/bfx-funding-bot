## Context

策略模組產生 `DecisionResult`，需要一個執行模組將其轉化為 Bitfinex REST API 呼叫。Offer Execution 負責掛單、撤單，並將每一筆操作記錄到 DB。

## Goals / Non-Goals

**Goals:**
- 接收 DecisionResult，依序執行 Cancels → Offers
- 每個 API 操作記錄 ExecutionRecord（成功/失敗都記）
- 解密使用者 API Key secret 再呼叫 Bitfinex
- 部分失敗容忍：不因單筆失敗中斷整批
- 回傳 ExecutionSummary 統計成功/失敗數

**Non-Goals:**
- 不做 credit renew（E2 負責）
- 不做批次到期（E3 負責）
- 不做利息再投資（E4 負責）
- 不做重試（呼叫者決定是否重試）

## Decisions

### 1. Consumer-side interface pattern

OfferExecutor 定義自己需要的 interface，不直接依賴 concrete types：

```go
type FundingClient interface {
    SubmitFundingOffer(ctx, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error)
    CancelFundingOffer(ctx, apiKey, apiSecret string, offerID int64) error
}

type KeyStore interface {
    GetByUserID(ctx, userID string) (*domain.APIKey, []byte, error)
}

type Cipher interface {
    Decrypt(ciphertext []byte) ([]byte, error)
}

type ExecutionLog interface {
    Create(ctx, record *domain.ExecutionRecord) (*domain.ExecutionRecord, error)
}
```

### 2. 執行順序

1. 從 KeyStore 取得使用者 API Key + 加密 secret
2. 用 Cipher 解密 secret
3. 先執行所有 Cancels（釋放資金）
4. 再執行所有 Offers（部署新掛單）
5. 每步都記 ExecutionRecord

### 3. 錯誤處理

- 取得 API Key 失敗 → 整批中斷，回傳錯誤
- 單筆 cancel 失敗 → 記錄失敗，繼續下一筆
- 單筆 offer 失敗 → 記錄失敗，繼續下一筆
- 回傳 ExecutionSummary 包含成功/失敗計數

## Risks / Trade-offs

- **[無重試]** → 簡單可預測，重試由上層 worker 決定
- **[序列執行]** → 不平行呼叫 API，避免 rate limit 問題
