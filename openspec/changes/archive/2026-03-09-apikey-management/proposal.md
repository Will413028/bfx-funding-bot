## Why

F5 Dashboard Layout 完成後，使用者需要管理 Bitfinex API Key（新增、驗證、刪除）。API Key 是啟動放貸引擎的前置條件，必須先完成這個頁面才能讓使用者開始自動放貸。

## What Changes

- 新增 `src/features/api-keys/hooks/use-api-keys.ts` — TanStack Query hooks（list、create mutation、delete mutation、verify mutation）
- 新增 `src/features/api-keys/components/api-key-card.tsx` — API Key 卡片（label、masked key、exchange status badge、funding balance、操作按鈕）
- 新增 `src/features/api-keys/components/create-apikey-dialog.tsx` — 新增 API Key 對話框（label + apiKey + apiSecret 表單）
- 新增 `src/features/api-keys/components/delete-confirm-dialog.tsx` — 刪除確認對話框
- 新增 `src/app/[locale]/(dashboard)/api-keys/page.tsx` — API Keys 管理頁面
- 安裝 shadcn/ui dialog 元件

## Capabilities

### New Capabilities
- `apikey-ui`: API Key 管理頁面元件群（CRUD hooks、卡片、對話框），premium dark theme

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/features/api-keys/` — 新增 hooks + components
- `frontend/src/app/[locale]/(dashboard)/api-keys/page.tsx` — 新增頁面
- `frontend/src/components/ui/dialog.tsx` — 新增（shadcn/ui）
- 依賴 F2 的 api-client, types, query-keys
- 依賴 F5 的 dashboard layout
- 後端 API: POST/GET/DELETE `/api-keys`, POST `/api-keys/:id/verify`
