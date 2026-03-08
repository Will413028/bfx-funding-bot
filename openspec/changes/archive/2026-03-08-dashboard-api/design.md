## Context

後端已有完整的 Bitfinex REST client（`GetFundingBalance`、`GetActiveFundingOffers`、`GetActiveFundingCredits`）和 API Key 加密儲存。Lending engine 每 3 分鐘自動運行，但使用者無法透過前端看到任何狀態。需要一個 Dashboard API 讓前端可以顯示即時 funding 狀態。

## Goals / Non-Goals

**Goals:**
- 提供單一 `GET /api/v1/dashboard` endpoint，回傳完整的 funding 摘要
- 聚合 wallet balance、active offers、active credits 於一次請求
- 回傳 engine readiness 狀態（使用者是否有 verified key + config）

**Non-Goals:**
- 不做歷史資料查詢（activity log 是獨立功能）
- 不做 WebSocket 即時推送（MVP 用前端 polling）
- 不快取 Bitfinex API 回應（每次都即時查詢）
- 不做 earnings 計算或統計

## Decisions

### 1. 單一聚合 endpoint vs 多個細分 endpoint

**選擇：** 單一 `GET /dashboard` endpoint

**理由：** 前端 dashboard 頁面需要同時顯示所有資訊，單一請求減少 round trips。後端透過 goroutine 平行呼叫 Bitfinex API 加速。

**替代方案：** 多個 endpoint（`/wallet`、`/offers`、`/credits`）— 更 RESTful 但增加前端複雜度和延遲。

### 2. Bitfinex API 呼叫策略

**選擇：** 平行呼叫三個 Bitfinex endpoint（wallets、offers、credits），使用 `errgroup` 管理

**理由：** 三個 API 呼叫互相獨立，平行執行可將延遲從 ~900ms（串列）降至 ~300ms（最慢的一個）。

### 3. 無 API Key 時的處理

**選擇：** 回傳空的 dashboard 結構（balance = 0, empty arrays）加上 `engine_ready: false`，不回傳錯誤

**理由：** 前端可以用 `engine_ready` flag 引導使用者設定 API Key 和 Config，而不是顯示錯誤頁面。

### 4. Service 層設計

**選擇：** 新增 `DashboardService`，依賴 `bitfinex.Client`、`APIKeyRepository`、`ConfigRepository`、`crypto.AES`

**理由：** 遵循既有的 handler → service → repository 三層架構。Dashboard 是讀取聚合，不需要自己的 repository。

## Risks / Trade-offs

- **[Rate Limit]** 每次 dashboard 請求觸發 3 個 Bitfinex API 呼叫 → 前端應設定合理的 polling 間隔（建議 30 秒以上）
- **[Latency]** Bitfinex API 延遲直接影響 dashboard 回應速度 → 使用 errgroup 平行呼叫緩解
- **[Partial Failure]** 某個 Bitfinex API 呼叫失敗時 → 回傳已成功取得的資料，失敗欄位設為 null/empty
