# E2E tests (Playwright)

```bash
pnpm test:e2e          # runs everything Playwright can run with the FE alone
E2E_FULL_STACK=1 pnpm test:e2e   # also checks signup/JWT/proxy containment
```

## Two tiers of tests

| Spec | Needs | Gated? |
|------|-------|--------|
| `smoke.spec.ts` | FE dev server only | no |
| `auth.spec.ts` → protected-route redirect | FE dev server only | no |
| `auth.spec.ts` → direct email signup denial | **full stack** (see below) | yes — `E2E_FULL_STACK` |
| `auth.spec.ts` → JWT endpoint/header boundary | **full stack** (see below) | yes — `E2E_FULL_STACK` |

The redirect test exercises the `getSessionCookie` middleware guard and runs
without any backend. The full-stack test sends a direct request to
`/api/auth/sign-up/email` and asserts the server hook rejects it with
`403 signup_disabled`. The JWT boundary tests assert that browser-visible
`/token`, `/sign-jwt`, and `/verify-jwt` routes return `404` and that
`/get-session` does not emit `set-auth-jwt`. These tests need the auth database
and Redis dependencies wired up and are skipped unless `E2E_FULL_STACK` is set.

## Running the full signup-containment test

`E2E_FULL_STACK=1 pnpm test:e2e`

### 1. Standalone Python web-API

The proxy forwards authenticated requests to the FastAPI web-API, which
verifies the Better-Auth-minted JWT against the Next app's JWKS endpoint.

```bash
cd backend && \
BETTER_AUTH_JWKS_URL=http://localhost:3000/api/auth/jwks \
uv run uvicorn bfx_funding_bot.apps.webapi:app
```

- `BETTER_AUTH_JWKS_URL` must point at the **running Next app**'s JWKS endpoint
  (served at `/api/auth/jwks`). The web-API also needs the same
  `BFX_OPERATOR_USER_ID` and `BFX_OPERATOR_ROLE=admin` as the frontend.
- Issuer + audience are **pinned stable strings**, decoupled from the deploy URL:
  the FE `jwt` plugin mints `iss="bfx-funding-bot"` / `aud="bfx-funding-backend"`
  (`src/lib/auth.ts`), and the BE defaults already match (`better_auth_issuer` /
  `jwt_audience` in `core/settings.py`). So you do **not** need to set
  `BETTER_AUTH_ISSUER` / `JWT_AUDIENCE` — override them only if you change the
  pinned strings in `auth.ts`.

### 2. Dev Postgres with the auth migration applied

Apply the Better Auth + `user_profile` migration to a **dev / throwaway**
Postgres (NOT live Neon):

```bash
cd backend && uv run alembic upgrade head   # against a dev DB, never prod
```

### 3. Frontend env

Point the FE at the dev DB and the web-API:

```bash
# Redis (sessions + rate-limit counters)
REDIS_URL=redis://localhost:6379

# Better Auth
BETTER_AUTH_SECRET=...                       # ≥ 32 chars
DATABASE_URL=postgresql://.../dev_db          # the dev Postgres, auth schema
BETTER_AUTH_URL=http://localhost:3000
NEXT_PUBLIC_BETTER_AUTH_URL=http://localhost:3000

# Proxy / server-action target
API_URL=http://localhost:8000                 # the standalone web-API
BFX_OPERATOR_USER_ID=operator-1               # must match the seeded admin user
BFX_OPERATOR_ROLE=admin
```

Then start the Next dev server (`pnpm dev`) and run
`E2E_FULL_STACK=1 pnpm test:e2e`.

> The protected-route redirect test runs without the backend; signup, JWT and
> public-proxy boundary tests are gated behind `E2E_FULL_STACK`.
