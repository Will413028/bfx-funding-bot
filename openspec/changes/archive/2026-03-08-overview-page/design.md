## Context

Overview 是 Dashboard 的首頁，呈現放貸平台的核心指標。資料來源：`GET /dashboard`（DashboardSummary）和 `GET /earnings`（EarningsSummary）。目前為 REST 輪詢，F12-F13 加入 WebSocket 後會即時推送。

## Goals / Non-Goals

**Goals:**
- Bento Box CSS Grid 佈局，打破傳統對稱
- 統計卡片展示核心指標（金額、收益、狀態）
- 金融數據排版（tabular-nums, % 弱化, USD 小數弱化）
- 進行中掛單列表 + 市場快照面板
- premium dark theme 暗黑卡片質感
- TanStack Query 資料層（hooks）

**Non-Goals:**
- 不實作 Recharts 圖表（F14）
- 不實作 WebSocket 即時推送（F12-F13）
- 不安裝 framer-motion 動畫（保持輕量，未來需要時再加）
- 不實作 number ticking 動畫（需 framer-motion）

## Decisions

### D1: Bento Box Grid 佈局

用 CSS Grid 實作非對稱佈局：
```
| Stat 1 | Stat 2 | Stat 3 | Stat 4 |   ← 4 cols (sm:2, lg:4)
| Chart Placeholder (col-span-full)  |   ← 寬區塊
| Offers List       | Market Panel   |   ← 2 cols (sm:1, lg:2)
```

### D2: StatCard 為共用元件

放在 `components/shared/`，不放 `features/dashboard/`，因為 F7-F10 頁面也可能用到統計卡片。接受 icon、label、value、subtitle、className props。

### D3: TanStack Query hooks 放在 features/dashboard/hooks/

符合 feature module 規範。useDashboard 和 useEarnings 各自獨立，可被 overview page 和未來其他頁面組合使用。

### D4: ui-financial-typography 規範

- 所有跳動數字的容器加 `tabular-nums tracking-tight`
- APY 為主角（較大字體 + emerald 色），日利率為輔助（較小 + muted）
- `%` 符號弱化：`<span className="text-zinc-500 text-sm">%</span>`
- USD 小數弱化：整數部分 `text-foreground`，小數部分 `text-zinc-500`

### D5: ui-premium-dark-theme 卡片

- 卡片背景：`bg-white/[0.02] border border-white/5`
- 頂部高光：`shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]`
- 狀態色：running → `text-emerald-400`，idle → `text-amber-500`，error → `text-rose-500`

## Risks / Trade-offs

- [REST 輪詢延遲] → staleTime 60s，使用者手動刷新可看到最新。F12-F13 加入 WS 後即時
- [圖表 placeholder] → 用灰色虛線框 + 文字提示，F14 替換
