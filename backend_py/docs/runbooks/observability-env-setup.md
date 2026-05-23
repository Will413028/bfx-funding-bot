# Observability Env Setup Runbook (free-plan 2-dataset variant)

Spec: [`2026-05-23-axiom-ci-env-separation-design.md`](../../../docs/superpowers/specs/2026-05-23-axiom-ci-env-separation-design.md)

> **Variant note.** The spec specifies 3 physically-separate datasets
> (prod / shadow / ci). The Axiom free plan caps dataset count, so this deploy
> uses **2 datasets** and relies on **logical** env separation via the
> `deployment_environment` envelope field instead of physical separation:
> prod and shadow **share** one dataset (distinguished by the env tag), and CI
> gets its own dataset to keep throwaway test events out of real traffic.
> Correctness is unchanged — emit tags every event and the query side filters
> `where deployment_environment == "<env>"`. See "Upgrading to the full
> 3-dataset split" below for the path once off the free plan.

## Two Axiom datasets

| Dataset | Use | `deployment_environment` values | Retention | Token env var |
|---|---|---|---|---|
| `bfx-funding-bot` | prod + shadow (real Koyeb services) | `prod`, `shadow` | existing (e.g. 30d) | `AXIOM_API_KEY` |
| `bfx-funding-bot-ci` | CI integration + local dev (throwaway) | `ci` | short (3-7d, or plan minimum) | `AXIOM_CI_API_KEY` / `AXIOM_API_KEY` |

## Creating the datasets + tokens (Axiom console)

**Datasets** — Settings → Datasets and views → New dataset, for each:
- **Name**: exactly `bfx-funding-bot-ci` (the prod/shadow `bfx-funding-bot`
  already exists). Must match `AXIOM_DATASET` exactly (case-sensitive).
- **Kind**: Events.
- **Retention**: short for ci; free plans may fix this — pick the minimum
  allowed.
- **Edge deployment**: default.

**Tokens** — Settings → API tokens → New API token:
- Pick **Advanced** → **Custom**, and for the target dataset grant **`ingest`
  + `query`**. A *Basic* token only ingests — insufficient, because both T12
  and the daemon's replay path also **query**.
- Scope each token to a single dataset (least privilege). Token privileges
  **cannot be edited after creation** — if wrong, delete and recreate.
- Copy the `xaat-...` token (shown once). The code sends it as
  `Authorization: Bearer <token>`.
- prod and shadow share the `bfx-funding-bot` dataset, so they can share one
  token, or use two tokens both scoped to `bfx-funding-bot` if you want
  independent revocation.

## Required env vars per env

| Var | prod | shadow | ci / local dev |
|---|---|---|---|
| `AXIOM_API_KEY` | `bfx-funding-bot` token | `bfx-funding-bot` token (same dataset) | ci token |
| `AXIOM_DATASET` | `bfx-funding-bot` | `bfx-funding-bot` | `bfx-funding-bot-ci` |
| `BFX_DEPLOYMENT_ENV` | `prod` | `shadow` | `ci` |
| `BFX_SERVICE_VERSION` | `$GIT_SHA` (Koyeb build arg) | `$GIT_SHA` | `$GIT_SHA` or auto from `git rev-parse HEAD` |

`BFX_DEPLOYMENT_ENV` is read by `AxiomConfig.from_env()` and must be exactly
one of `prod` / `shadow` / `ci` — anything else fails loud at daemon boot
(`ValueError`). `build_daemon` threads the resolved env into BOTH the emit
client (`AxiomClient` via `EventResource`) and the replay query adapter
(`AxiomReplayQueryAdapter`), so a process always emits with and queries by the
same env tag. **prod and shadow point at the same dataset** — their isolation
is the `deployment_environment` tag, not the dataset.

## Local dev `.env` example

```env
# Local integration test against the Axiom ci dataset
AXIOM_API_KEY=xaat_xxx_ci_scoped_token
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

- `AXIOM_CI_API_KEY` = ci-dataset-scoped Axiom token (advanced: ingest+query)
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

## Koyeb env setup (no dataset migration needed)

prod and shadow already share `bfx-funding-bot`, so there is **no dataset move**
— just set the correct `BFX_DEPLOYMENT_ENV` on each service so their events get
tagged distinctly.

- **prod service** env: `AXIOM_DATASET=bfx-funding-bot`,
  `AXIOM_API_KEY=<bfx-funding-bot token>`, `BFX_DEPLOYMENT_ENV=prod`.
- **shadow service** env: `AXIOM_DATASET=bfx-funding-bot`,
  `AXIOM_API_KEY=<bfx-funding-bot token>`, `BFX_DEPLOYMENT_ENV=shadow`.

Save → Koyeb auto-redeploys. Verify (5-15 min observe window):
- Koyeb logs show `deployment_env=DeploymentEnvironment.PROD` / `.SHADOW` on boot.
- Axiom: events carry the right tag. APL to confirm the split:
  ```kusto
  ['bfx-funding-bot'] | where _time > ago(1h) | summarize count() by deployment_environment
  ```
  should show separate `prod` and `shadow` buckets.

Rollback if needed: Koyeb console → env vars → revert `BFX_DEPLOYMENT_ENV`.

## Phase 4.4 canary cutover (future)

1. Confirm the env tags are present and correct (prod + shadow share the
   dataset by design, so do NOT expect a "0 non-prod rows" check):
   ```kusto
   ['bfx-funding-bot'] | where _time > ago(7d) | summarize count() by deployment_environment
   ```
2. GitHub Settings → Branches → `main` → require status check
   `backend_py_integration`.
3. Koyeb prod service env:
   - `AXIOM_DATASET` → `bfx-funding-bot`
   - `AXIOM_API_KEY` → `<bfx-funding-bot token>`
   - `BFX_DEPLOYMENT_ENV` → `prod`
   - `BFX_EXECUTOR` → `bitfinex_live`
   - `BFX_WS_CLIENT_ENABLED` → `true`

## Upgrading to the full 3-dataset split (when off the free plan)

No code change required — only deployment config:
1. Axiom console → create `bfx-funding-bot-shadow` (retention 30d) + an
   advanced token (ingest+query) scoped to it.
2. Koyeb **shadow** service → set `AXIOM_DATASET=bfx-funding-bot-shadow` +
   `AXIOM_API_KEY=<shadow token>` (keep `BFX_DEPLOYMENT_ENV=shadow`).
3. Save → redeploy. Shadow events now land in their own dataset; the
   `deployment_environment` tag keeps working, and you gain physical isolation
   + independent retention. (Optionally backfill is unnecessary — old shadow
   rows remain queryable in `bfx-funding-bot` filtered by the tag.)

## Troubleshooting

- **`ValueError: BFX_DEPLOYMENT_ENV required`** — env var unset; export it.
- **`ValueError: 'staging' is not a valid DeploymentEnvironment`** — only
  `prod` / `shadow` / `ci` are valid.
- **prod and shadow events look mixed in one dataset** — expected in this
  free-plan variant; they share `bfx-funding-bot` and are separated by the
  `deployment_environment` tag. Use the `summarize count() by
  deployment_environment` APL above to confirm tags are present.
- **T12 `Axiom indexing timeout: got 0/3 after 30s`** — check (a) the ci token
  has both ingest AND query (an ingest-only Basic token can't read back),
  (b) Axiom service status, (c) the APL `deployment_environment == "ci"` clause
  matches the emitted env.
- **CI fork-PR integration job not running** — by design (`if:` clause); fork
  PRs cannot access secrets per the GitHub security model.
