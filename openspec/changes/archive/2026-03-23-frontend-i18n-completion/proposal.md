## Why

多個前端元件含有硬編碼英文字串，未走 i18n 翻譯流程。這導致 zh-TW locale 下仍顯示英文，破壞雙語體驗的一致性。已知的硬編碼字串包括：

- `query-error.tsx`: "Failed to load data", "Try again"
- `market-panel.tsx`: "Market Snapshot"
- `strategy-form.tsx`: "Auto Renew"
- `offers-list.tsx`: "Active Offers"
- 其他 dashboard 頁面標題與 UI label

## What Changes

- 審計所有前端元件中的硬編碼字串
- 新增對應的 i18n keys 到 `en.json` 和 `zh-TW.json`
- 將硬編碼字串替換為 `useTranslations()` 呼叫

## Capabilities

### New Capabilities

（無——純 i18n 補齊）

### Modified Capabilities

- 所有 dashboard 元件完整支援 zh-TW locale

## Impact

- `frontend/messages/en.json` — 新增 keys
- `frontend/messages/zh-TW.json` — 新增 keys
- 多個 `features/*/components/*.tsx` — 替換硬編碼字串
- `components/shared/query-error.tsx` — 替換硬編碼字串
