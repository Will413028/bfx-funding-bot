## 1. 審計

- [x] 1.1 搜索所有 `.tsx` 檔案中的硬編碼英文字串
- [x] 1.2 完成清單：92 個字串跨 21 個檔案

## 2. 新增 i18n keys

- [x] 2.1 在 `en.json` 新增所有缺少的 keys（app, overview, apiKeys, strategy, settings, history, common, auth namespaces）
- [x] 2.2 在 `zh-TW.json` 新增對應的中文翻譯

## 3. 替換硬編碼字串

- [x] 3.1 Dashboard 元件（6 檔）：stats-grid, market-panel, offers-list, earnings-chart, rate-chart, setup-checklist
- [x] 3.2 API Keys + Strategy 元件（4 檔）：api-key-card, create-apikey-dialog, delete-confirm-dialog, strategy-form
- [x] 3.3 Settings + History + Shared + Layout 元件（8 檔）：profile-card, change-password-form, execution-table, billing-table, query-error, load-more-button, topbar, marketing-header
- [x] 3.4 Page + Auth 元件（7 檔）：overview/page, api-keys/page, strategy/page, settings/page, history/page, login-form, register-form

## 4. 驗證

- [x] 4.1 `biome check` — 0 新增錯誤（3 pre-existing lint warnings 與本次無關）
- [x] 4.2 `biome format` — 90 files, no fixes needed
