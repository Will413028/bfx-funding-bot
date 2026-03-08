## Why

F3 完成了 API Proxy 和 login/register/logout Server Actions，但 UI 還是 placeholder。需要真正的登入與註冊頁面，讓使用者能與後端互動。這是進入 Dashboard 頁面（F5-F10）的前置條件。

## What Changes

- 安裝 shadcn/ui 元件：Button, Input, Label, Card
- 新增 `src/lib/validations.ts` — auth 表單 Zod schema（loginSchema, registerSchema）
- 新增 `src/features/auth/components/login-form.tsx` — LoginForm client component（react-hook-form + zod + login server action + callbackUrl redirect）
- 新增 `src/features/auth/components/register-form.tsx` — RegisterForm client component（react-hook-form + zod + register server action）
- 更新 `src/app/[locale]/(auth)/login/page.tsx` — 使用 LoginForm
- 新增 `src/app/[locale]/(auth)/register/page.tsx` — 使用 RegisterForm
- 更新 `src/app/[locale]/(auth)/layout.tsx` — 套用 ui-premium-dark-theme 暗黑質感

## Capabilities

### New Capabilities
- `auth-ui`: 登入/註冊表單元件，Zod 驗證，i18n 翻譯，premium dark theme

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/components/ui/` — 新增 shadcn/ui 元件（button, input, label, card）
- `frontend/src/lib/validations.ts` — 新增
- `frontend/src/features/auth/components/` — 新增 login-form, register-form
- `frontend/src/app/[locale]/(auth)/` — 更新 layout, login/page, 新增 register/page
- 依賴 F3 的 login/register/logout Server Actions
