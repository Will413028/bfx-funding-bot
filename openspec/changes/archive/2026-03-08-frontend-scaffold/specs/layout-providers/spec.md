## ADDED Requirements

### Requirement: Root layout
The `src/app/layout.tsx` SHALL be a pass-through layout that:
- Imports `globals.css`
- Sets metadata (title: "BFX Funding Bot", description)
- Renders `{children}` without wrapping `<html>` or `<body>` (delegated to locale layout)

#### Scenario: Root layout renders children
- **WHEN** any page is loaded
- **THEN** the root layout passes children through to the locale layout

### Requirement: Locale layout with providers
The `src/app/[locale]/layout.tsx` SHALL:
- Render `<html lang={locale} className="... dark">` with Inter font
- Wrap children with providers in order: NextIntlClientProvider > QueryProvider > NuqsAdapter
- Call `setRequestLocale(locale)` for static rendering support
- Export `generateStaticParams` returning all locales
- Validate locale with `hasLocale`, calling `notFound()` if invalid

#### Scenario: Locale layout renders with correct language
- **WHEN** visiting `/zh-TW/overview`
- **THEN** the `<html>` tag has `lang="zh-TW"`

#### Scenario: Invalid locale returns 404
- **WHEN** visiting `/fr/overview`
- **THEN** the not-found page is displayed

### Requirement: QueryProvider
The `src/providers/query-provider.tsx` SHALL:
- Be a `"use client"` component
- Create QueryClient inside `useState` (not module-level, to prevent SSR cache leaking)
- Set default `staleTime: 60_000` and `retry: 1`
- Include ReactQueryDevtools (collapsed by default)

#### Scenario: QueryProvider prevents SSR cache sharing
- **WHEN** two different users load pages on the server
- **THEN** each request gets its own QueryClient instance

### Requirement: Global error boundary
The `src/app/global-error.tsx` SHALL catch unhandled errors at the top level and display a fallback UI with a retry button.

#### Scenario: Unhandled error shows fallback
- **WHEN** an unhandled error occurs in any page
- **THEN** the global error boundary displays an error message with a "Try again" button

### Requirement: Not-found page
The `src/app/[locale]/not-found.tsx` SHALL display a localized 404 page.

#### Scenario: Unknown route shows 404
- **WHEN** visiting `/en/nonexistent-page`
- **THEN** a 404 page is displayed

### Requirement: Route group skeletons
The project SHALL create three route groups under `src/app/[locale]/` with minimal placeholder pages:

**`(marketing)/`**:
- `layout.tsx` — placeholder layout (renders children)
- `page.tsx` — placeholder home page

**`(auth)/`**:
- `layout.tsx` — centered layout
- `login/page.tsx` — placeholder login page

**`(dashboard)/`**:
- `layout.tsx` — placeholder layout (renders children)
- `overview/page.tsx` — placeholder overview page
- `loading.tsx` — loading skeleton
- `error.tsx` — error boundary

#### Scenario: Marketing page accessible
- **WHEN** visiting `/en/`
- **THEN** the marketing placeholder page renders

#### Scenario: Auth page accessible
- **WHEN** visiting `/en/login`
- **THEN** the login placeholder page renders (or redirects if authenticated)

#### Scenario: Dashboard page accessible when authenticated
- **WHEN** an authenticated user visits `/en/overview`
- **THEN** the overview placeholder page renders

### Requirement: next.config.ts
The `next.config.ts` SHALL:
- Apply the next-intl plugin via `createNextIntlPlugin()`
- NOT import Sentry (Phase 3)
- Include commented-out Sentry configuration for future reference

#### Scenario: Next config loads correctly
- **WHEN** starting the dev server
- **THEN** the next-intl plugin is applied and routing works
