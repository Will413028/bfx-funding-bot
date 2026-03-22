## Why

Next.js App Router 的 `(dashboard)` route group 有 `error.tsx` 錯誤邊界，但 `(auth)` 和 `(marketing)` route groups 沒有。當這兩個區塊的頁面拋出未捕獲的錯誤時，會直接 fallback 到 `global-error.tsx`，使用者體驗不佳（整頁白屏 + 需要重新整理）。

## What Changes

- 新增 `(auth)/error.tsx` — 登入/註冊頁面的錯誤邊界，提供「回到首頁」按鈕
- 新增 `(marketing)/error.tsx` — 公開頁面的錯誤邊界，提供「重試」按鈕
- 兩者都支援 i18n，使用 dark theme 風格

## Capabilities

### New Capabilities

- `auth-error-boundary`: 認證頁面的局部錯誤恢復
- `marketing-error-boundary`: 公開頁面的局部錯誤恢復

### Modified Capabilities

（無）

## Impact

- `frontend/src/app/[locale]/(auth)/error.tsx` — 新增
- `frontend/src/app/[locale]/(marketing)/error.tsx` — 新增
