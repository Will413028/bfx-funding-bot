## 1. Project Initialization

- [x] 1.1 Create `frontend/` directory, run `pnpm create next-app@latest` with App Router + TypeScript + Tailwind v4 + `src/` directory + pnpm
- [x] 1.2 Remove ESLint config and eslint dependencies (replaced by Biome)
- [x] 1.3 Install all Phase 1 dependencies: zustand, @tanstack/react-query, @tanstack/react-query-devtools, nuqs, react-hook-form, @hookform/resolvers, zod, next-intl, class-variance-authority, tailwind-merge, clsx, tailwindcss-animate, lucide-react
- [x] 1.4 Install dev dependencies: @biomejs/biome, knip

## 2. Configuration Files

- [x] 2.1 Create `biome.json` with formatter + linter config (space indent, double quotes, recommended rules, noUnusedVariables off)
- [x] 2.2 Create `knip.config.ts` for unused code detection
- [x] 2.3 Create `components.json` for shadcn/ui (new-york style, Tailwind v4 mode, path aliases)
- [x] 2.4 Verify `postcss.config.mjs` has `@tailwindcss/postcss` plugin
- [x] 2.5 Create `.env.example` with all required environment variables
- [x] 2.6 Update `package.json` scripts (dev, build, start, lint, format, knip)

## 3. Theme & Styling

- [x] 3.1 Configure `src/app/globals.css` — Tailwind v4 entry (`@import "tailwindcss"`), dark financial theme CSS variables, `@theme` mapping, `@plugin "tailwindcss-animate"`, `@custom-variant dark`
- [x] 3.2 Create `src/lib/utils.ts` with `cn()` function (clsx + tailwind-merge)

## 4. Internationalization (next-intl)

- [x] 4.1 Create `src/i18n/routing.ts` — defineRouting with locales ["en", "zh-TW"], defaultLocale "en"
- [x] 4.2 Create `src/i18n/request.ts` — getRequestConfig with dynamic message import
- [x] 4.3 Create `src/i18n/navigation.ts` — createNavigation exports (Link, redirect, usePathname, useRouter)
- [x] 4.4 Create `frontend/middleware.ts` — next-intl routing + auth protection (protected paths redirect to login, auth pages redirect to overview)
- [x] 4.5 Create `messages/en.json` with all namespaces (nav, common, auth, overview, apiKeys, strategy, history, validation, status)
- [x] 4.6 Create `messages/zh-TW.json` with matching namespaces
- [x] 4.7 Update `next.config.ts` — apply createNextIntlPlugin(), add commented Sentry config for Phase 3

## 5. Layouts & Providers

- [x] 5.1 Create `src/providers/query-provider.tsx` — "use client", QueryClient in useState, ReactQueryDevtools
- [x] 5.2 Update `src/app/layout.tsx` — pass-through root layout (import globals.css, metadata, render children only)
- [x] 5.3 Create `src/app/[locale]/layout.tsx` — locale layout with Inter font, html lang={locale} dark class, provider chain (NextIntlClientProvider > QueryProvider > NuqsAdapter), generateStaticParams, setRequestLocale
- [x] 5.4 Create `src/app/[locale]/not-found.tsx` — localized 404 page
- [x] 5.5 Create `src/app/global-error.tsx` — top-level error boundary with retry button

## 6. Route Group Skeletons

- [x] 6.1 Create `src/app/[locale]/(marketing)/layout.tsx` + `page.tsx` — placeholder home page
- [x] 6.2 Create `src/app/[locale]/(auth)/layout.tsx` — centered layout
- [x] 6.3 Create `src/app/[locale]/(auth)/login/page.tsx` — placeholder login page
- [x] 6.4 Create `src/app/[locale]/(dashboard)/layout.tsx` — placeholder layout (renders children)
- [x] 6.5 Create `src/app/[locale]/(dashboard)/overview/page.tsx` — placeholder overview page
- [x] 6.6 Create `src/app/[locale]/(dashboard)/loading.tsx` — loading skeleton
- [x] 6.7 Create `src/app/[locale]/(dashboard)/error.tsx` — dashboard error boundary

## 7. Verification

- [x] 7.1 Run `pnpm build` — verify production build succeeds
- [x] 7.2 Run `pnpm lint` — verify tsc + Biome pass
- [x] 7.3 Run `pnpm dev` — verify dev server starts, test locale routing (/en/, /zh-TW/), verify middleware redirects
