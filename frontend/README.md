# BFX Funding Bot frontend

Next.js 16 App Router frontend for the Release 0 operator-only deployment.
Self-service signup is disabled; private API calls go through the same-origin
BFF and only receive a server-side EdDSA execution JWT after fresh session and
per-session MFA checks.

## Development

```bash
pnpm install
pnpm dev
pnpm test
pnpm lint
pnpm build
```

The frontend uses `NEXT_PUBLIC_*` values at build time. Server-only values
(`API_URL`, Better Auth/Postgres/Redis secrets and operator ID/role) are
supplied at runtime; `src/lib/env.ts` validates them during Node startup.

## E2E

```bash
pnpm test:e2e
E2E_FULL_STACK=1 pnpm test:e2e
```

The full-stack tier covers signup denial, JWT endpoint/header containment and
the exact anonymous public proof proxy allowlist. See [`e2e/README.md`](e2e/README.md)
for the disposable Postgres/Redis setup.

## Deployment

The current stack runs on the Oracle Cloud VM through Docker Compose. Use the
root [`scripts/deploy-vm.sh`](../scripts/deploy-vm.sh) preflight and follow the
[Release 0 operator containment runbook](../docs/runbooks/release-0-operator-containment.md).
