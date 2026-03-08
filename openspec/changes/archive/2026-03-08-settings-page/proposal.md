## Why

Dashboard 各功能頁面（Overview、API Keys、Strategy、History）已完成，最後需要 Settings 頁面讓使用者檢視帳戶資訊和修改密碼。這是 Dashboard 五個核心頁面的最後一個。

## What Changes

- 新增 `src/features/settings/hooks/use-user.ts` — TanStack Query hook（GET /me）+ 修改密碼 mutation（PUT /me/password）
- 新增 `src/features/settings/components/profile-card.tsx` — 帳戶資訊卡片（email、status、plan badge、createdAt）
- 新增 `src/features/settings/components/change-password-form.tsx` — 修改密碼表單（currentPassword + newPassword + confirmPassword）
- 新增 `src/app/[locale]/(dashboard)/settings/page.tsx` — Settings 頁面
- 新增 Zod 驗證 schema（密碼 match + min 8 chars）

## Capabilities

### New Capabilities
- `settings-ui`: Settings 頁面元件群（profile card、change password form、hooks），premium dark theme

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/features/settings/` — 新增 hooks + components
- `frontend/src/app/[locale]/(dashboard)/settings/page.tsx` — 新增頁面
- `frontend/src/lib/validations.ts` — 新增 changePasswordSchema
- 依賴 F2 的 api-client, types
- 依賴 F5 的 dashboard layout
- 後端 API: GET `/me`, PUT `/me/password`
