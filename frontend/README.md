# BFX Funding Bot frontend

Next.js 16 App Router operator UI, built as a static export (9d-5). There is no Next
server: the web API serves `out/` behind Tailscale Serve, and the browser calls the same
origin's `/api/v1`. Who may use it is decided by the tailnet policy (the operator app
capability Serve forwards), not by a login in the UI.

## Development

```bash
pnpm install
pnpm dev          # the UI alone; API calls fail without a web API (see e2e/README.md)
pnpm test
pnpm lint
pnpm build        # writes out/
```

The only build input is `NEXT_PUBLIC_SENTRY_DSN` (optional; `.env.example`). Browser
Sentry events go to the web API's `/fe-events`, which forwards them to that DSN's project.

## E2E

```bash
pnpm test:e2e
```

See [`e2e/README.md`](e2e/README.md) for running against the web API with the export.

## Deployment

There is no frontend image: `backend/Dockerfile`'s `ui` stage builds this directory (the
named build context `frontend` in `.github/workflows/release.yml`) into the backend image,
and the web API serves it from `/app/static`. The VM deploys that image by digest with
`bfx-deploy` (see the [deploy runbook](../docs/runbooks/deploy.md)).
