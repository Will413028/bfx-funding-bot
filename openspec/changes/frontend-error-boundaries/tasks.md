## 1. 參考現有 error boundary

- [x] 1.1 讀取 `(dashboard)/error.tsx` 作為參考模板
- [x] 1.2 確認 i18n key 結構 — `common.error` 已存在

## 2. 實作

- [x] 2.1 新增 `(auth)/error.tsx` — 顯示錯誤訊息 + 「回到首頁」Link，i18n 支援
- [x] 2.2 新增 `(marketing)/error.tsx` — 顯示錯誤訊息 + 「重試」按鈕，i18n 支援
- [x] 2.3 新增 i18n keys：`common.tryAgain`、`common.unexpectedError`、`common.backToHome`（en.json + zh-TW.json）
- [x] 2.4 順便修正 `(dashboard)/error.tsx` 硬編碼字串改用 i18n

## 3. 驗證

- [x] 3.1 `biome check` 通過
- [x] 3.2 `pnpm lint` — pre-existing TS errors（@testing-library/react 缺型別宣告），與本次改動無關
