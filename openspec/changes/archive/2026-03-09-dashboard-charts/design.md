## Context

後端 `EarningsService.GetEarnings()` 已會從 Bitfinex 拉取 `[]FundingEarning`（含 Amount + Timestamp），但只做彙總。需要新增方法按日分桶回傳 time-series。

前端 Overview 頁面有 ChartPlaceholder 等待替換。WebSocket 提供即時 FRR 數據。

## Goals / Non-Goals

**Goals:**
- 後端：`GET /api/v1/earnings/history?days=30` 回傳每日收益 time-series
- 前端：Recharts 收益走勢 AreaChart + FRR 即時走勢 AreaChart
- 替換 ChartPlaceholder，premium dark theme 一致

**Non-Goals:**
- 不持久化收益到 DB（每次從 Bitfinex API 拉取）
- 不做歷史利率持久化
- 不做日期範圍選擇器（固定 30 天）

## Decisions

### D1: Earnings History API

```
GET /api/v1/earnings/history?days=30
Response: {
  "data": [
    { "date": "2026-03-09", "amount": 12.50 },
    { "date": "2026-03-08", "amount": 8.30 },
    ...
  ]
}
```

- `days` 參數可選，預設 30，上限 90
- Service 呼叫 `bitfinex.GetFundingEarnings(start, end)` 取得原始記錄
- 按 UTC 日期分桶加總，填充無收益的日期為 0
- 回傳按日期升序排列

### D2: Service 方法

在 `EarningsService` 新增 `GetEarningsHistory(ctx, userID, days) ([]DailyEarning, error)`。
邏輯：取得 API key → 解密 → 呼叫 Bitfinex → 按日分桶 → 回傳。

```go
type DailyEarning struct {
    Date   string  `json:"date"`   // "2026-03-09"
    Amount float64 `json:"amount"`
}
```

### D3: 前端 Earnings Chart

- 新增 `useEarningsHistory()` hook（TanStack Query，staleTime 5 分鐘）
- Recharts `AreaChart`，X 軸日期，Y 軸 USD 金額
- Gradient fill emerald-500，軸線 zinc-700

### D4: 前端 Rate Chart

- 從 `useWSStore` 訂閱 snapshot，用 `useRef` 累積 FRR 到 ring buffer（max 60 筆）
- Recharts `AreaChart`，X 軸時間，Y 軸 APR%
- 無數據時顯示 "Waiting for market data..."

### D5: 圖表樣式

- 背景透明，premium dark card 外框
- 軸線 zinc-700，label 文字 zinc-400
- Area gradient: emerald-500/20 → transparent
- ResponsiveContainer 100% 寬，高度 200px
- 2-column grid layout 替換原 ChartPlaceholder

### D6: 前端 Types

```typescript
interface DailyEarning {
  date: string;
  amount: number;
}
```

## Risks / Trade-offs

- [Bitfinex API 限制] → 每次都從 Bitfinex 拉取，有 API rate limit。staleTime 5 分鐘緩解
- [Rate Chart 短暫] → 僅頁面開啟期間累積，離開即消失。未來可持久化
- [90 天上限] → Bitfinex 歷史查詢有限制，超過可能需分頁拉取。30 天預設避開此問題
