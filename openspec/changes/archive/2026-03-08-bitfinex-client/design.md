## Context

CRUD 層完成後，需要與 Bitfinex 交互。架構文件規定 `internal/bitfinex/` 封裝交易所 API，上層不直接操作 HTTP 請求。MVP 只做 REST API，WebSocket 留到 lending engine。

目前 API Key 已加密儲存在 DB，bitfinex client 需要接收解密後的 key/secret 來建立連線。

## Goals / Non-Goals

**Goals:**
- 自行實作 Bitfinex REST API v2 client（HMAC-SHA384 簽名）
- 支援 Funding 相關操作：驗證權限、查餘額、提交/撤銷掛單、查詢 active credits
- 定義 funding 相關的 domain models（Offer、Credit、Wallet）
- 提供可測試的架構（HTTP client 可注入 mock）

**Non-Goals:**
- WebSocket 連線管理（ws.go）— 留到 lending engine
- 公開市場數據拉取（marketfeed）— 留到 lending engine
- Rate limiting / retry — 留到 lending engine
- HTTP API endpoints 暴露 bitfinex 功能 — 這是 internal 依賴

## Decisions

### 1. 自行實作而非使用官方 SDK

**選擇**: 用 Go stdlib (`net/http` + `crypto/hmac`) 直接呼叫 Bitfinex REST API v2。

**替代方案**: 使用 `bitfinex-api-go/v2` 官方 SDK。

**理由**: 官方 SDK 更新不穩定，依賴較重。Bitfinex REST API v2 的認證機制簡單（HMAC-SHA384），只需少量 funding 相關 endpoints，自行實作更輕量、可控。

### 2. HMAC-SHA384 認證流程

Bitfinex API v2 authenticated endpoints 需要三個 HTTP headers：
- `bfx-apikey`: API Key
- `bfx-nonce`: 毫秒級 Unix timestamp（單調遞增）
- `bfx-signature`: HMAC-SHA384(`/api/v2/<path><nonce><body>`, secret)

將簽名邏輯封裝在 `bitfinex/auth.go`，與業務邏輯分離。

### 3. `http.Client` 可注入

**選擇**: `bitfinex.Client` 接收 `*http.Client` 作為依賴，測試時可替換為 `httptest.Server`。

**理由**: 避免在單元測試中發送真實 HTTP 請求。Integration test 可使用真實 client。

### 4. Per-call credential

**選擇**: 每個方法接收 `apiKey, apiSecret string` 參數。

**理由**: MVP 階段低頻操作，不需要 per-user client pool。未來 lending engine 可改為 per-user authenticated client。

### 5. 錯誤處理

**選擇**: Bitfinex API 錯誤回應轉換為 `domain.AppError`，包含 HTTP status + 原始錯誤訊息。

**理由**: 與現有錯誤處理模式一致，上層可直接傳遞給 handler。

## Risks / Trade-offs

- **[API 版本變化]** → Bitfinex API v2 相對穩定。若有 breaking change，只需修改 `bitfinex/` package。
- **[Nonce collision]** → 使用 `time.Now().UnixMilli()` + atomic counter 避免併發時 nonce 重複。
- **[無 rate limiting]** → MVP 可接受，Bitfinex API 會回 429，上層可處理。
