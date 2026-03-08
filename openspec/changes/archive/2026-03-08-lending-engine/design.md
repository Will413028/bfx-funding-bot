## Context

所有前置條件已就緒：用戶可以儲存 API Key（已驗證）、設定放貸策略（currency、amount、rate、period）、系統可以透過 `bitfinex.Client` 操作 Bitfinex API。`strategy_specification.md` 定義了完整策略系統，本 change 實作 MVP 引擎框架，為後續進階模組打基礎。

## Goals / Non-Goals

**Goals:**
- 定期（每 3 分鐘）掃描所有活躍用戶，執行放貸策略
- 根據 `StrategyConfig` 的 rate/amount/period 範圍決定掛單參數
- 管理 active offers：餘額可用時提交新 offer，避免重複掛單
- 僵屍單 TTL：超過 15 分鐘未成交的 offer 自動撤銷（spec §7.1）
- 利息即時再投資：可用餘額 >= 50 USD 即掛單（spec §7.1）
- 基礎安全：餘額 < 50 USD 待機（spec §8.2）
- 透過 fx lifecycle 管理引擎生命週期
- 可測試的架構（依賴注入）

**Non-Goals:**
- WebSocket 即時市場數據（§3, §6.1 事件心跳）
- 複合信號決策框架 MDC（§2）
- 資金分層佈署（§4.2）
- 隱藏單、深度感知拆分（§4.3, §4.6）
- 動態天數決策 / 利率曲線分析（§5）
- 績效追蹤（§9）
- Rate limiting / retry — MVP 失敗跳過等下一輪

## Decisions

### 1. Ticker-based 背景循環（spec §6.1 簡化版）

**選擇**: 使用 `time.Ticker` + goroutine，固定間隔 3 分鐘。

**對應 spec**: §6.1 定時心跳（活躍期 3 分鐘），但省略事件心跳和動態間隔。

**理由**: 最簡單的方式。事件心跳需要 WebSocket，留到後續。3 分鐘間隔在 rate limit 內。

### 2. 逐用戶串行執行

**選擇**: 每輪循環中逐用戶串行處理，單用戶失敗不影響其他用戶。

**理由**: MVP 用戶數少，串行處理夠快。避免 nonce 衝突。後續可改為 worker pool（spec §10 多租戶架構）。

### 3. MVP 策略邏輯

每輪循環對每個用戶執行：
1. 查詢 funding wallet 可用餘額
2. 查詢 active offers
3. **僵屍單 TTL 撤銷**（spec §7.1）：active offer 存在超過 15 分鐘 → 撤銷
4. **掛單決策**：
   - 可用餘額 < 50 USD → 跳過（spec §8.2 防呆）
   - 有 active offer 且未超時 → 跳過
   - 否則提交新 offer
5. **掛單參數**：
   - rate = config.Rate.Min（保守策略，確保成交。後續用 MDC 動態調整）
   - amount = min(可用餘額, config.Amount.Max)
   - period = config.Period.Min

**對應 spec**: §7.1 動態複利循環的簡化版。省略隊首保留、原子化換單、深度感知拆分。

### 4. Engine struct 設計

```go
type Engine struct {
    bfx       *bitfinex.Client
    apiKeyRepo repository.APIKeyRepository
    configRepo repository.ConfigRepository
    cipher    *crypto.AES
    log       *zap.Logger
    interval  time.Duration
    cancel    context.CancelFunc
}
```

- `Start(ctx)` 啟動 ticker goroutine
- `Stop()` 取消 context，等待 goroutine 結束
- `tick(ctx)` 單次循環：遍歷用戶 → processUser
- `processUser(ctx, apiKey, config)` 單用戶處理

### 5. 用戶遍歷方式

**選擇**: 查詢所有有 verified API Key 的用戶，再查詢其 strategy config。兩者都有的才執行。

**理由**: 簡單直接。MVP 不需要用戶列表表，直接從 api_keys + user_configs 表交叉查詢。

需要新增 repo 方法：`APIKeyRepository.ListVerified(ctx)` — 返回所有 exchange_status="verified" 的 API Keys（含 encrypted secret）。

### 6. 錯誤處理

**選擇**: 單用戶失敗只記 log，不中斷其他用戶。引擎本身的 panic 由 recover 保護。

**對應 spec**: §6.3 優雅降級的簡化版。

## Risks / Trade-offs

- **[API Rate Limit]** → 3 分鐘間隔 + 每用戶 ~4 req。90 req/min 限制下可支撐 ~60 用戶。
- **[Nonce 衝突]** → 串行處理避免。
- **[掛單重複]** → 每輪檢查 active offers。
- **[無動態定價]** → MVP 用 config.Rate.Min 固定利率，不做市場分析。
- **[無歷史記錄]** → MVP 不記錄掛單歷史。
- **[僵屍單 TTL 固定]** → 固定 15 分鐘，spec §7.1 定義活躍期 10-15 分 / 死水期 45-60 分，MVP 先用固定值。
