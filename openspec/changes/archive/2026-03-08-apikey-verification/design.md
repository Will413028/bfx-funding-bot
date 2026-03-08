## Context

目前 `APIKeyService.Create` 只做加密儲存，不驗證 key 有效性。`bitfinex.Client` 已實作 `VerifyCredentials` 和 `GetFundingBalance` 方法，可以直接使用。

現有流程：用戶 POST API Key → 加密 → 存 DB → 回傳（不驗證）
目標流程：用戶 POST API Key → 呼叫 Bitfinex API 驗證 → 加密 → 存 DB（含驗證狀態）→ 回傳（含餘額資訊）

## Goals / Non-Goals

**Goals:**
- 儲存 API Key 時自動驗證有效性
- 提供獨立驗證端點（前端可先驗證再儲存）
- 追蹤 key 的驗證狀態（verified / unverified）
- 驗證成功時回傳 funding wallet 餘額

**Non-Goals:**
- 定期重新驗證 key — 留到 lending engine
- 驗證 key 的具體權限（funding permission check）— MVP 只驗證 key 是否有效
- 前端 UI — 只做後端 API

## Decisions

### 1. 驗證失敗仍允許儲存

**選擇**: 驗證失敗時仍然儲存 key，但標記為 `unverified`。

**替代方案**: 驗證失敗時拒絕儲存。

**理由**: Bitfinex API 可能暫時不可用（rate limit、維護），不應阻止用戶儲存 key。用戶可以之後透過獨立驗證端點重新驗證。

### 2. 在 Service 層注入 bitfinex.Client

**選擇**: `APIKeyService` 新增 `*bitfinex.Client` 依賴，在 `Create` 方法中呼叫驗證。

**理由**: 驗證是業務邏輯，屬於 service 層。不在 handler 層做是因為需要解密 secret 才能驗證。

### 3. exchange_status 欄位

**選擇**: `api_keys` 表新增 `exchange_status TEXT NOT NULL DEFAULT 'unverified'`。

**理由**: 簡單的狀態欄位足以追蹤。不需要額外的驗證歷史表。

### 4. 獨立驗證端點設計

**選擇**: `POST /api-keys/:id/verify`，驗證已儲存的 key 並更新狀態。

**替代方案**: `POST /api-keys/verify` 接受 raw key/secret（不儲存）。

**理由**: 驗證已儲存的 key 更安全（不需要在 request body 傳遞 secret），也能更新 DB 狀態。

## Risks / Trade-offs

- **[Bitfinex API 不可用]** → 驗證失敗不阻止儲存，標記為 unverified
- **[額外延遲]** → Create 流程多一次 Bitfinex API 呼叫（~200-500ms），可接受
- **[Rate limiting]** → Bitfinex API 90 req/min，單次驗證不會觸發
