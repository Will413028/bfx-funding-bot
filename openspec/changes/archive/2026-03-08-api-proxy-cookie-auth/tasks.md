## 1. API Proxy Route

- [x] 1.1 Create `src/app/api/proxy/[...path]/route.ts` — catch-all proxy with cookie→Bearer conversion, query param preservation, response passthrough, export GET/POST/PUT/DELETE

## 2. Auth Server Actions

- [x] 2.1 Create `src/app/[locale]/(auth)/actions.ts` — login action (call backend /auth/login, set HttpOnly cookie on success, return error on failure)
- [x] 2.2 Add register action (call backend /auth/register, auto-login on success)
- [x] 2.3 Add logout action (delete cookie, redirect to login)

## 3. Verification

- [x] 3.1 Run `pnpm build` — verify production build succeeds
- [x] 3.2 Run `pnpm lint` — verify tsc + Biome pass
