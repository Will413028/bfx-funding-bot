## 1. Validations

- [x] 1.1 Add change password Zod schema to `src/lib/validations.ts` — currentPassword (required), newPassword (required, min 8), confirmPassword (required), refine match

## 2. Data Hooks

- [x] 2.1 Create `src/features/settings/hooks/use-user.ts` — useUser query (GET /me) + useChangePassword mutation (PUT /me/password)

## 3. Components

- [x] 3.1 Create `src/features/settings/components/profile-card.tsx` — email, status badge (active/suspended), plan badge, createdAt, premium dark theme
- [x] 3.2 Create `src/features/settings/components/change-password-form.tsx` — react-hook-form + Zod, 3 password fields, success/error feedback, form reset on success

## 4. Page

- [x] 4.1 Create `src/app/[locale]/(dashboard)/settings/page.tsx` — "use client", useUser, ProfileCard + ChangePasswordForm, loading state

## 5. Verification

- [x] 5.1 Run `pnpm build` — verify production build succeeds
- [x] 5.2 Run `pnpm lint` — verify tsc + Biome pass
