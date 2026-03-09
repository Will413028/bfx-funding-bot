## 1. i18n

- [x] 1.1 Add marketing translation keys to `messages/en.json` and `messages/zh-TW.json` — landing (hero, features, cta), pricing (plans, features)

## 2. Layout Components

- [x] 2.1 Create `src/components/layout/marketing-header.tsx` — logo, Pricing nav link, Login button, premium dark theme
- [x] 2.2 Create `src/components/layout/marketing-footer.tsx` — copyright text
- [x] 2.3 Create `src/app/[locale]/(marketing)/layout.tsx` — MarketingHeader + footer + main content area

## 3. Pages

- [x] 3.1 Create `src/app/[locale]/(marketing)/page.tsx` — Server Component, hero section + 4 feature cards (responsive grid) + bottom CTA, i18n
- [x] 3.2 Create `src/app/[locale]/(marketing)/pricing/page.tsx` — Server Component, 4 plan cards (Free/Starter/Pro/Enterprise), Pro highlighted, feature checklists, CTA buttons, i18n

## 4. Verification

- [x] 4.1 Run `pnpm build` — verify production build succeeds
- [x] 4.2 Run `pnpm lint` — verify tsc + Biome pass
