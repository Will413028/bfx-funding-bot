## Context

Auth pages 是使用者進入平台的第一道 UI。需要展現「機構級量化放貸 SaaS」的暗黑質感，同時保持極簡——只有 email + password 兩個欄位。F3 已完成 Server Actions（login, register, logout），本次只需建立 client component 呼叫它們。

## Goals / Non-Goals

**Goals:**
- 可用的登入/註冊表單（含 client-side Zod 驗證 + server-side 錯誤顯示）
- 套用 ui-premium-dark-theme 視覺規範
- i18n 完整支援（error message 用 key → t() 翻譯）
- 登入成功後依 callbackUrl 導向（預設 /overview）

**Non-Goals:**
- 不實作忘記密碼 / 社群登入（未來增強）
- 不實作 email 驗證流程
- 不安裝 framer-motion（F4 不需要動畫，F5/F6 再加）

## Decisions

### D1: shadcn/ui 元件用 CLI 安裝

用 `npx shadcn@latest add button input label card` 安裝，自動處理 Tailwind v4 相容。這些是最基礎的元件，後續 phase 也會用到。

### D2: Form 用 react-hook-form + @hookform/resolvers/zod

已在 F1 安裝。Zod schema 放在 `lib/validations.ts`，錯誤訊息用英文 key（`"required"`, `"invalidEmail"`），表單元件用 `t(`validation.${error}`)` 翻譯。

### D3: Feature-based 元件結構

LoginForm / RegisterForm 放在 `features/auth/components/`，符合架構文件的 feature module 規範。Page 檔案保持精簡——只 import form component + 傳入翻譯。

### D4: ui-premium-dark-theme 套用方式

- Auth layout：全螢幕置中，`bg-zinc-950` 底色
- Card 容器：`bg-white/[0.02] border border-white/5 backdrop-blur-xl`
- 按鈕：primary 用 `bg-emerald-600 hover:bg-emerald-500`（收益綠，暗示「進入生財系統」）
- Input：`bg-zinc-900/50 border-white/10`

### D5: callbackUrl 處理

LoginForm 從 URL search params 讀取 `callbackUrl`（middleware 在重導向至 login 時設定），登入成功後用 `router.push(callbackUrl || "/overview")` 導向。使用 nuqs 的 `useQueryState` 或直接用 `useSearchParams`。

## Risks / Trade-offs

- [shadcn/ui CLI 可能修改 globals.css] → 安裝後檢查，必要時還原 CSS 變數
- [Zod v4 strict mode] → 表單 schema 只驗證預期欄位，不會有問題
