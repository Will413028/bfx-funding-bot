## Why

使用者需要查閱放貸執行歷史和帳單紀錄，了解引擎做了什麼操作、每月費用多少。後端已提供 cursor-based pagination 的 GET /executions 和 GET /billing API，前端需要一個 History 頁面以 Tab 切換兩個表格呈現。

## What Changes

- 新增 `src/features/history/hooks/use-executions.ts` — TanStack Query infinite query + cursor pagination
- 新增 `src/features/history/hooks/use-billing.ts` — TanStack Query infinite query + cursor pagination
- 新增 `src/features/history/components/execution-table.tsx` — 執行紀錄表格
- 新增 `src/features/history/components/billing-table.tsx` — 帳單紀錄表格
- 新增 `src/components/shared/load-more-button.tsx` — 共用 Load More 按鈕
- 新增 `src/app/[locale]/(dashboard)/history/page.tsx` — History 頁面（Tab 切換）
- 安裝 shadcn/ui tabs 元件

## Capabilities

### New Capabilities
- `history-ui`: History 頁面元件群（infinite query hooks、execution/billing 表格、Tab 切換），premium dark theme

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/features/history/` — 新增 hooks + components
- `frontend/src/components/shared/` — 新增 load-more-button
- `frontend/src/app/[locale]/(dashboard)/history/page.tsx` — 新增頁面
- `frontend/src/components/ui/tabs.tsx` — 新增（shadcn/ui）
- 依賴 F2 的 api-client, types, query-keys, format
- 依賴 F5 的 dashboard layout
- 後端 API: GET `/executions?after=&limit=`, GET `/billing?after=&limit=`
