## Why

Dashboard 功能頁面已全部完成（F6-F10），但目前未登入使用者沒有入口頁面。需要 Landing page 介紹產品、展示功能特色，以及 Pricing page 呈現方案比較表，引導使用者註冊。

## What Changes

- 新增 `src/app/[locale]/(marketing)/layout.tsx` — Marketing layout（header + footer）
- 新增 `src/app/[locale]/(marketing)/page.tsx` — Landing page（hero + features + CTA）
- 新增 `src/app/[locale]/(marketing)/pricing/page.tsx` — Pricing page（4 方案比較表）
- 新增 `src/components/layout/marketing-header.tsx` — Marketing header（logo + nav + login 按鈕）
- 新增 `src/components/layout/marketing-footer.tsx` — Marketing footer

## Capabilities

### New Capabilities
- `marketing-ui`: Marketing 頁面（Landing + Pricing），marketing layout（header + footer），premium dark theme

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/app/[locale]/(marketing)/` — 新增 layout + 2 頁面
- `frontend/src/components/layout/` — 新增 marketing-header, marketing-footer
- 獨立於 Dashboard，不依賴 API 或 TanStack Query
- 方案資料 hardcoded（來自 ROADMAP.md 訂閱方案表）
