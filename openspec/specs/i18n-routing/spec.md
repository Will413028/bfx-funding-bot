# i18n-routing Specification

## Purpose
TBD - created by archiving change frontend-scaffold. Update Purpose after archive.
## Requirements
### Requirement: Locale routing configuration
The project SHALL define supported locales in `src/i18n/routing.ts` using `defineRouting` with:
- Locales: `["en", "zh-TW"]`
- Default locale: `"en"`

#### Scenario: URL includes locale prefix
- **WHEN** a user visits `/en/overview` or `/zh-TW/overview`
- **THEN** the page renders with the correct locale

#### Scenario: Root URL redirects to default locale
- **WHEN** a user visits `/`
- **THEN** the user is redirected to `/en/`

### Requirement: Request configuration
The project SHALL have `src/i18n/request.ts` that:
- Uses `getRequestConfig` from `next-intl/server`
- Validates the requested locale against the routing config
- Falls back to the default locale if invalid
- Dynamically imports the matching translation JSON from `messages/`

#### Scenario: Valid locale loads translations
- **WHEN** a page is requested with locale `zh-TW`
- **THEN** the translations from `messages/zh-TW.json` are loaded

#### Scenario: Invalid locale falls back
- **WHEN** a page is requested with locale `fr` (unsupported)
- **THEN** the default locale `en` is used

### Requirement: Navigation utilities
The project SHALL export locale-aware navigation utilities from `src/i18n/navigation.ts` using `createNavigation`:
- `Link` — locale-aware anchor component
- `redirect` — locale-aware redirect function
- `usePathname` — current pathname without locale prefix
- `useRouter` — locale-aware router

#### Scenario: Link component preserves locale
- **WHEN** rendering `<Link href="/overview">` in a `zh-TW` page
- **THEN** the link points to `/zh-TW/overview`

### Requirement: Middleware with auth protection
The `middleware.ts` at `frontend/` root SHALL:
- Apply next-intl locale routing to all non-API paths
- Redirect unauthenticated users from protected paths to login (with `callbackUrl`)
- Redirect authenticated users from auth pages to overview
- Protected paths: `/overview`, `/api-keys`, `/strategy`, `/history`, `/settings`
- Auth pages: `/login`, `/register`
- Auth detection: check `auth_token` cookie existence

#### Scenario: Unauthenticated user visits dashboard
- **WHEN** a user without `auth_token` cookie visits `/en/overview`
- **THEN** the user is redirected to `/en/login?callbackUrl=/overview`

#### Scenario: Authenticated user visits login
- **WHEN** a user with `auth_token` cookie visits `/en/login`
- **THEN** the user is redirected to `/en/overview`

#### Scenario: Public pages accessible without auth
- **WHEN** any user visits `/en/` (marketing) or `/en/pricing`
- **THEN** the page renders without redirect

### Requirement: Translation file skeleton
The project SHALL include skeleton translation files at `messages/en.json` and `messages/zh-TW.json` with the following namespaces:
- `nav` — navigation labels
- `common` — shared UI strings (loading, error, save, cancel, etc.)
- `auth` — login/register labels
- `overview` — dashboard overview labels
- `apiKeys` — API key management labels
- `strategy` — strategy config labels
- `history` — history page labels
- `validation` — form validation messages
- `status` — status labels (running, paused, connected, etc.)

#### Scenario: All namespaces present
- **WHEN** importing `messages/en.json`
- **THEN** all listed namespaces exist with at least the keys defined in `frontend_architecture.md`

