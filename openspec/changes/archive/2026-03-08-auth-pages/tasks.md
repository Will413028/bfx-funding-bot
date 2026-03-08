## 1. shadcn/ui Components

- [x] 1.1 Install shadcn/ui components: `npx shadcn@latest add button input label card` — verify globals.css not overwritten
- [x] 1.2 Verify installed components render correctly with existing dark theme CSS variables

## 2. Validation Schemas

- [x] 2.1 Create `src/lib/validations.ts` — `loginSchema` (email required + valid format, password required) and `registerSchema` (email required + valid format, password min 8 chars), error messages as English keys for i18n
- [x] 2.2 Add missing validation keys to `messages/en.json` and `messages/zh-TW.json` (e.g. `minLength`, `invalidPassword`)

## 3. Auth Form Components

- [x] 3.1 Create `src/features/auth/components/login-form.tsx` — "use client", react-hook-form + zodResolver, call login server action, show server errors, redirect on success (callbackUrl from searchParams, default /overview), loading state on submit button
- [x] 3.2 Create `src/features/auth/components/register-form.tsx` — "use client", react-hook-form + zodResolver, call register server action, show server errors, redirect on success, loading state

## 4. Pages & Layout

- [x] 4.1 Update `src/app/[locale]/(auth)/layout.tsx` — apply premium dark theme (bg-zinc-950, card container with bg-white/[0.02] border-white/5 backdrop-blur-xl)
- [x] 4.2 Update `src/app/[locale]/(auth)/login/page.tsx` — render LoginForm with i18n translations, link to register
- [x] 4.3 Create `src/app/[locale]/(auth)/register/page.tsx` — render RegisterForm with i18n translations, link to login

## 5. Verification

- [x] 5.1 Run `pnpm build` — verify production build succeeds
- [x] 5.2 Run `pnpm lint` — verify tsc + Biome pass
