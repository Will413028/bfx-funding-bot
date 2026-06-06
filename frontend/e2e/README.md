# E2E tests (Playwright)

```bash
pnpm test:e2e          # runs everything Playwright can run with the FE alone
E2E_FULL_STACK=1 pnpm test:e2e   # also runs the full happy-path test
```

## Two tiers of tests

| Spec | Needs | Gated? |
|------|-------|--------|
| `smoke.spec.ts` | FE dev server only | no |
| `auth.spec.ts` → protected-route redirect | FE dev server only | no |
| `auth.spec.ts` → sign-up happy path | **full stack** (see below) | yes — `E2E_FULL_STACK` |

The redirect test exercises the `getSessionCookie` middleware guard and runs
without any backend. The happy-path test signs a user up, lands on
`/en/overview`, and asserts an authenticated `/api/proxy/...` GET returns 200 —
so it needs the entire auth chain wired up. It is skipped unless
`E2E_FULL_STACK` is set.

## Running the full happy-path test

`E2E_FULL_STACK=1 pnpm test:e2e`

### 1. Standalone Python web-API

The proxy forwards authenticated requests to the FastAPI web-API, which
verifies the Better-Auth-minted JWT against the Next app's JWKS endpoint.

```bash
cd backend_py && \
BETTER_AUTH_JWKS_URL=http://localhost:3000/api/auth/jwks \
BETTER_AUTH_ISSUER=http://localhost:3000 \
JWT_AUDIENCE=bfx-funding-backend \
uv run uvicorn bfx_funding_bot.main:app
```

- `BETTER_AUTH_JWKS_URL` / `BETTER_AUTH_ISSUER` point at the **running Next app**
  (the JWKS is served at `/api/auth/jwks`; the issuer is the Better Auth base URL).
- `JWT_AUDIENCE` must match the `audience` minted in `src/lib/auth.ts`
  (`bfx-funding-backend`).

### 2. Dev Postgres with the auth migration applied

Apply the Better Auth + `user_profile` migration to a **dev / throwaway**
Postgres (NOT live Neon):

```bash
cd backend_py && uv run alembic upgrade head   # against a dev DB, never prod
```

### 3. Frontend env

Point the FE at the dev DB and the web-API:

```bash
# Upstash (sessions + rate-limit counters)
UPSTASH_REDIS_REST_URL=...
UPSTASH_REDIS_REST_TOKEN=...

# Better Auth
BETTER_AUTH_SECRET=...                       # ≥ 32 chars
DATABASE_URL=postgresql://.../dev_db          # the dev Postgres, auth schema
BETTER_AUTH_URL=http://localhost:3000
NEXT_PUBLIC_BETTER_AUTH_URL=http://localhost:3000

# Proxy / server-action target
API_URL=http://localhost:8000                 # the standalone web-API
```

Then start the Next dev server (`pnpm dev`) and run
`E2E_FULL_STACK=1 pnpm test:e2e`.

> The protected-route redirect test runs without the backend; only the
> sign-up happy-path test is gated behind `E2E_FULL_STACK`.
