## 1. Validations

- [x] 1.1 Add strategy config Zod schema to `src/lib/validations.ts` — APR percentage inputs, cross-field min ≤ max refinements for amount/rate/period

## 2. Data Hooks

- [x] 2.1 Create `src/features/strategy/hooks/use-config.ts` — useConfig query (404 → null), useSaveConfig mutation (PUT, APR→daily conversion), useResetConfig mutation (DELETE), invalidate configKeys.all

## 3. Components

- [x] 3.1 Create `src/features/strategy/components/strategy-form.tsx` — react-hook-form + Zod, three sections (basic + ranges + actions), APR display, default values, save/reset with confirmation, premium dark theme cards

## 4. Page

- [x] 4.1 Create `src/app/[locale]/(dashboard)/strategy/page.tsx` — "use client", useConfig, StrategyForm, loading/error states

## 5. Verification

- [x] 5.1 Run `pnpm build` — verify production build succeeds
- [x] 5.2 Run `pnpm lint` — verify tsc + Biome pass
