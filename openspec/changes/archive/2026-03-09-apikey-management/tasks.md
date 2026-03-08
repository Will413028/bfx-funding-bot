## 1. Dependencies

- [x] 1.1 Install shadcn/ui dialog: `npx shadcn@latest add dialog` + auto-fix Biome

## 2. Validations

- [x] 2.1 Add API Key Zod schema to `src/lib/validations.ts` — label (required, 1-50 chars), apiKey (required), apiSecret (required)

## 3. Data Hooks

- [x] 3.1 Create `src/features/api-keys/hooks/use-api-keys.ts` — useApiKeys query + useCreateApiKey / useDeleteApiKey / useVerifyApiKey mutations (invalidate apiKeyKeys.all on success)

## 4. Components

- [x] 4.1 Create `src/features/api-keys/components/api-key-card.tsx` — label, masked key, exchange status badge (verified/unverified/failed colors), funding balance, Verify + Delete buttons
- [x] 4.2 Create `src/features/api-keys/components/create-apikey-dialog.tsx` — react-hook-form + Zod, label + apiKey + apiSecret fields, loading state, close on success
- [x] 4.3 Create `src/features/api-keys/components/delete-confirm-dialog.tsx` — confirmation message with key label, Delete + Cancel buttons, loading state

## 5. Page

- [x] 5.1 Create `src/app/[locale]/(dashboard)/api-keys/page.tsx` — "use client", useApiKeys, list cards, empty state, Create button (hidden when key exists), loading/error states

## 6. Verification

- [x] 6.1 Run `pnpm build` — verify production build succeeds
- [x] 6.2 Run `pnpm lint` — verify tsc + Biome pass
