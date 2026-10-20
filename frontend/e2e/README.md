# E2E tests (Playwright)

```bash
pnpm test:e2e
```

`playwright.config.ts` starts `pnpm dev`, so the specs run against the UI alone:
`smoke.spec.ts` checks the pages render, and `uncertainty.spec.ts` mocks the API with
`page.route` (`**/api/v1/...`). Nothing here needs a database or a login.

## Against the web API (the production shape)

In production the web API serves the static export and Tailscale Serve adds the operator's
identity. Locally there is no Serve, so the browser sends the identity headers itself; the
web API listens on 127.0.0.1 only, so a forged header locally is no risk.

1. Build the export: `pnpm build` (writes `out/`).
2. Start the web API against a dev database (never prod) with the export and the ingress
   settings (`backend/README.md` lists them):

   ```bash
   cd ../backend && BFX_WEBAPI_STATIC_DIR=../frontend/out \
     BFX_OPERATOR_TAILNET_USER_ID=operator-1 BFX_WEBAPI_ALLOWED_HOSTS=localhost \
     BFX_FE_SENTRY_DSN=https://key@o1.ingest.sentry.io/1 \
     uv run uvicorn bfx_funding_bot.apps.webapi:app --host 127.0.0.1 --port 8000
   ```

3. Point Playwright at it and send the headers Serve would add, e.g. in a local config:
   `use: { baseURL: "http://localhost:8000", extraHTTPHeaders: { "Tailscale-App-Capabilities":
   '{"github.com/Will413028/bfx-funding-bot/cap/operator":[{"user_id":"operator-1"}]}' } }`
   and no `webServer`. The operator also needs a membership row with `user_id = operator-1`
   in the dev database to see an account.
