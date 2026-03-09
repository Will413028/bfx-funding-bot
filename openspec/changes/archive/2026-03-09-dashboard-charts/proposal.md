## Why

Overview 頁面有一個 ChartPlaceholder（"Charts coming soon — Phase F14"），需要替換成實際圖表。後端目前只提供彙總收益（7d/30d 總額），缺少按日分桶的 time-series endpoint。需要先補後端 API，再建前端圖表。

## What Changes

**Backend:**
- 新增 `GET /api/v1/earnings/history?days=30` — 從 Bitfinex 拉取 FundingEarning 記錄，按日分桶回傳 `[{date, amount}]`
- Service 層新增 `GetEarningsHistory()` 方法

**Frontend:**
- 安裝 Recharts 套件
- 新增收益走勢圖 — AreaChart 顯示每日收益（從 earnings/history API）
- 新增利率即時折線圖 — 累積 WebSocket FRR 數據到 ring buffer
- 替換 ChartPlaceholder 為實際圖表元件

## Capabilities

### New Capabilities
- `earnings-history-api`: 後端每日收益 time-series endpoint
- `dashboard-charts`: 前端收益走勢圖 + 利率即時走勢圖（Recharts）

### Modified Capabilities
_None_

## Impact

- **後端新增**: `service/earnings.go`（GetEarningsHistory 方法）, `handler/earnings.go`（History handler）, `handler/router.go`（新路由）
- **前端新增**: `rate-chart.tsx`, `earnings-chart.tsx`, hooks
- **前端修改**: `overview/page.tsx`
- **前端刪除**: `chart-placeholder.tsx`
- **依賴**: 新增 `recharts`（前端）
