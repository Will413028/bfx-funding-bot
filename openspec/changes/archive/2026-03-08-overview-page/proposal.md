## Why

F5 完成了 Dashboard 佈局框架，但 Overview 頁面仍是 placeholder。Overview 是使用者登入後的首頁，需要一目瞭然展示放貸狀態：錢包餘額、進行中掛單、預估日收益、市場快照。這是整個 Dashboard 的核心頁面。

## What Changes

- 新增 `src/features/dashboard/hooks/use-dashboard.ts` — TanStack Query hook 取得 DashboardSummary
- 新增 `src/features/dashboard/hooks/use-earnings.ts` — TanStack Query hook 取得 EarningsSummary
- 新增 `src/components/shared/stat-card.tsx` — 可復用統計卡片元件（icon + label + value + subtitle）
- 新增 `src/features/dashboard/components/stats-grid.tsx` — 4 張統計卡片 Bento Box 網格（Total Lent, Available Balance, Est. Daily Earning, Worker Status）
- 新增 `src/features/dashboard/components/offers-list.tsx` — 進行中掛單面板
- 新增 `src/features/dashboard/components/market-panel.tsx` — 市場快照面板（FRR, regime, MDC, flash freeze）
- 新增 `src/features/dashboard/components/chart-placeholder.tsx` — 圖表區域 placeholder（F14 再換成 Recharts）
- 更新 `src/app/[locale]/(dashboard)/overview/page.tsx` — 組合以上元件
- 安裝 shadcn/ui Badge 元件（放貸天數、狀態標籤用）

## Capabilities

### New Capabilities
- `overview-ui`: Overview 頁面元件群（統計卡片、掛單列表、市場快照、圖表 placeholder），Bento Box 佈局，金融數據排版規範

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/features/dashboard/` — 新增 hooks + components
- `frontend/src/components/shared/` — 新增 stat-card
- `frontend/src/components/ui/badge.tsx` — 新增（shadcn/ui）
- `frontend/src/app/[locale]/(dashboard)/overview/page.tsx` — 重寫
- 依賴 F2 的 api-client, types, query-keys, format
- 依賴 F5 的 dashboard layout
