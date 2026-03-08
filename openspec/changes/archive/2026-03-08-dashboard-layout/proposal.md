## Why

F4 完成了 Auth Pages，登入後會導向 `/overview`，但 Dashboard 目前只是空殼（直接 render children）。需要建立完整的 Dashboard 佈局——側邊欄導航 + 頂部列 + main content area，這是 F6-F10 所有 Dashboard 頁面的共用框架。

## What Changes

- 安裝 shadcn/ui 元件：Separator, Sheet（mobile sidebar drawer）, DropdownMenu（語系切換 + 使用者選單）
- 新增 `src/components/layout/sidebar.tsx` — 導航側邊欄（overview, api-keys, strategy, history, settings）+ active 狀態高亮 + lucide icons + logout action + mobile responsive
- 新增 `src/components/layout/topbar.tsx` — 頂部列（logo + mobile menu toggle + 語系切換 + 使用者選單）
- 新增 `src/components/layout/locale-switcher.tsx` — 語系切換元件（en ↔ zh-TW）
- 更新 `src/app/[locale]/(dashboard)/layout.tsx` — 組合 sidebar + topbar + main content area
- 更新 `src/app/[locale]/(dashboard)/loading.tsx` — skeleton 適配新 layout

## Capabilities

### New Capabilities
- `dashboard-shell`: Dashboard 佈局框架（sidebar + topbar + main），mobile responsive，premium dark theme

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/components/layout/` — 新增 sidebar, topbar, locale-switcher
- `frontend/src/components/ui/` — 新增 separator, sheet, dropdown-menu (shadcn/ui)
- `frontend/src/app/[locale]/(dashboard)/layout.tsx` — 重寫
- `frontend/src/app/[locale]/(dashboard)/loading.tsx` — 更新
- 所有 Dashboard 子頁面（F6-F10）將自動套用此佈局
