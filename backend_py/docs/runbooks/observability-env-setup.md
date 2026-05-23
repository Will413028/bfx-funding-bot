# Observability Env Setup Runbook

Spec: [`2026-05-23-axiom-ci-env-separation-design.md`](../../../docs/superpowers/specs/2026-05-23-axiom-ci-env-separation-design.md)

## Three Axiom datasets

| Dataset | Use | Retention | Token env var |
|---|---|---|---|
| `bfx-funding-bot` | prod (Phase 4.4 canary) | 30d | `AXIOM_API_KEY` (prod scope) |
| `bfx-funding-bot-shadow` | Koyeb shadow service | 30d | `AXIOM_API_KEY` (shadow scope) |
| `bfx-funding-bot-ci` | CI integration + local dev | 3-7d | `AXIOM_CI_API_KEY` / `AXIOM_API_KEY` (ci scope) |

## Required env vars per env

| Var | prod | shadow | ci / local dev |
|---|---|---|---|
| `AXIOM_API_KEY` | prod token | shadow token | ci token |
| `AXIOM_DATASET` | `bfx-funding-bot` | `bfx-funding-bot-shadow` | `bfx-funding-bot-ci` |
| `BFX_DEPLOYMENT_ENV` | `prod` | `shadow` | `ci` |
| `BFX_SERVICE_VERSION` | `$GIT_SHA` (Koyeb build arg) | `$GIT_SHA` | `$GIT_SHA` or auto from `git rev-parse HEAD` |

`BFX_DEPLOYMENT_ENV` is read by `AxiomConfig.from_env()` and must be exactly
one of `prod` / `shadow` / `ci` — anything else fails loud at daemon boot
(`ValueError`). `build_daemon` threads the resolved env into BOTH the emit
client (`AxiomClient` via `EventResource`) and the replay query adapter
(`AxiomReplayQueryAdapter`), so a process always emits to and queries the same
dataset env.

## Local dev `.env` example

```env
# Local integration test against the Axiom ci dataset
AXIOM_API_KEY=axk_xxx_ci_scoped_token
AXIOM_DATASET=bfx-funding-bot-ci
BFX_DEPLOYMENT_ENV=ci
BFX_SERVICE_VERSION=  # leave empty — auto-resolves via git rev-parse
```

Then run integration tests locally:

```bash
cd backend_py && uv run pytest -m integration -v
```

Without these creds the round-trip test (`test_axiom_replay_round_trip`) skips
cleanly; the rest of the integration suite runs against testcontainers / mocks.

## CI configuration

GitHub Actions repo secrets (Settings → Secrets and variables → Actions):

- `AXIOM_CI_API_KEY` = ci-dataset-scoped Axiom token
- `AXIOM_CI_DATASET` = `bfx-funding-bot-ci`

`.github/workflows/ci.yml` `backend_py_integration` job wires these into env
automatically (`BFX_DEPLOYMENT_ENV=ci`, `BFX_SERVICE_VERSION=${{ github.sha }}`,
`GITHUB_RUN_ID` for run-scoped account_id isolation). Fork PRs skip the job (no
secret access). If the job runs before the secrets exist, the round-trip test
skips and the job still passes on the testcontainer/mock tests.

## Dockerfile / Koyeb build arg

`backend_py/Dockerfile` declares `ARG GIT_SHA=unknown` and exports
`ENV BFX_SERVICE_VERSION=${GIT_SHA}`. The Koyeb build pipeline must pass the
commit SHA as a build arg, e.g. `--build-arg GIT_SHA=$KOYEB_GIT_COMMIT` (verify
the exact Koyeb-provided variable name in the Koyeb docs). If unset, the image
falls back to `BFX_SERVICE_VERSION=unknown`.

## Koyeb shadow service migration

1. Axiom console → create `bfx-funding-bot-shadow` dataset (retention 30d) +
   generate a dataset-scoped token (shadow scope).
2. Koyeb console → marketfeed shadow service → Edit env vars:
   - `AXIOM_DATASET` → `bfx-funding-bot-shadow`
   - `AXIOM_API_KEY` → `<shadow_scope_token>`
   - `BFX_DEPLOYMENT_ENV` → `shadow`
3. Save → Koyeb auto-redeploys.
4. Verify (5-15 min observe window):
   - Koyeb logs: `AxiomConfig(deployment_env=DeploymentEnvironment.SHADOW)` on boot
   - Axiom console: `bfx-funding-bot-shadow` dataset receives events
   - Axiom console: `bfx-funding-bot` dataset stops receiving events from shadow
5. Rollback if needed: Koyeb console → env vars → revert to prod dataset values.

## Phase 4.4 canary cutover (future)

1. Verify `bfx-funding-bot` dataset query
   `where _time > ago(30d) and deployment_environment != "prod"` returns 0 rows
   (30d after the Koyeb shadow cutover).
2. GitHub Settings → Branches → `main` → require status check
   `backend_py_integration`.
3. Koyeb prod service env:
   - `AXIOM_DATASET` → `bfx-funding-bot`
   - `AXIOM_API_KEY` → `<prod_scope_token>`
   - `BFX_DEPLOYMENT_ENV` → `prod`
   - `BFX_EXECUTOR` → `bitfinex_live`
   - `BFX_WS_CLIENT_ENABLED` → `true`

## Troubleshooting

- **`ValueError: BFX_DEPLOYMENT_ENV required`** — env var unset; export it.
- **`ValueError: 'staging' is not a valid DeploymentEnvironment`** — only
  `prod` / `shadow` / `ci` are valid.
- **T12 `Axiom indexing timeout: got 0/3 after 30s`** — check (a) the ci dataset
  token has write permission, (b) Axiom service status, (c) the APL
  `deployment_environment == "ci"` clause matches the emitted env.
- **CI fork-PR integration job not running** — by design (`if:` clause); fork
  PRs cannot access secrets per the GitHub security model.
