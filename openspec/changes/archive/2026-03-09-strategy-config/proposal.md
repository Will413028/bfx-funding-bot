## Why

使用者新增 API Key 後，需要設定放貸策略參數（金額範圍、利率範圍、天數範圍、自動續約）。Strategy Config 是啟動放貸引擎的第二個前置條件。後端已提供 GET/PUT/DELETE /configs API，前端需要一個表單頁面讓使用者設定。

## What Changes

- 新增 `src/features/strategy/hooks/use-config.ts` — TanStack Query hooks（get、save mutation、reset mutation）
- 新增 `src/features/strategy/components/strategy-form.tsx` — 策略表單（currency + autoRenew + amount/rate/period min-max 輸入 + save/reset 按鈕）
- 新增 `src/app/[locale]/(dashboard)/strategy/page.tsx` — Strategy 設定頁面
- 新增 Zod 驗證 schema（min ≤ max 跨欄位驗證）

## Capabilities

### New Capabilities
- `strategy-ui`: 策略設定頁面（表單、hooks、驗證），premium dark theme

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/features/strategy/` — 新增 hooks + components
- `frontend/src/app/[locale]/(dashboard)/strategy/page.tsx` — 新增頁面
- `frontend/src/lib/validations.ts` — 新增 strategyConfigSchema
- 依賴 F2 的 api-client, types, query-keys
- 依賴 F5 的 dashboard layout
- 後端 API: GET/PUT/DELETE `/configs`
